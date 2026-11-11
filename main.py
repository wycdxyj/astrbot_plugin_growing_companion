"""AstrBot 插件入口：拟人化成长陪伴 Bot（growing_companion）。

本文件是唯一依赖 AstrBot 的地方；全部拟人化逻辑在 core/ 下，可脱离 AstrBot 单测。
LLM / TTS / STT 一律走 AstrBot 后台已配置的供应商，插件内不含任何密钥/模型名/地址。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

try:                                   # AstrBot 新版：StarTools.get_data_dir()
    from astrbot.api.star import StarTools
except ImportError:                    # pragma: no cover
    StarTools = None  # type: ignore[assignment]

try:
    from .core.brainmap.export import export_graph
    from .core.brainmap.page import render_page
    from .core.brainmap.server import BrainMapServer
    from .core.engine import CompanionEngine
    from .core.life_tick import LifeTick
    from .core.llm import AstrBotLLM
    from .core import readonly_views as views
    from .core.speech import AstrBotSTT, build_tts
    from .core.expression import ReplySender, SessionGate
except ImportError:                    # 个别加载方式不是包导入时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core.brainmap.export import export_graph          # type: ignore[no-redef]
    from core.brainmap.page import render_page              # type: ignore[no-redef]
    from core.brainmap.server import BrainMapServer         # type: ignore[no-redef]
    from core.engine import CompanionEngine                 # type: ignore[no-redef]
    from core.life_tick import LifeTick                     # type: ignore[no-redef]
    from core.llm import AstrBotLLM                         # type: ignore[no-redef]
    from core import readonly_views as views                # type: ignore[no-redef]
    from core.speech import AstrBotSTT, build_tts          # type: ignore[no-redef]
    from core.expression import ReplySender, SessionGate

PLUGIN_NAME = "growing_companion"
PLUGIN_VERSION = "4.0.0"
DENY = "这个只有爸爸才能看哦～"
TAG = "[growing_companion]"          # 日志前缀：在 AstrBot 日志里搜它，就能看出插件有没有在工作
PREFIX_CHARS = "/!！／"
# 只读查看命令（长的在前，避免前缀互相吞掉）。on_message 里按名字自己识别，不依赖 AstrBot 的唤醒前缀设置
COMMANDS = ("记忆详情", "记忆列表", "成长报告", "关系状态", "最近复盘", "她状态", "脑图")


def parse_command(text: str) -> tuple[str, str, bool] | None:
    """识别“[/]命令名 [参数]”，返回 (命令名, 参数, 是否带前缀)；不是我们的命令返回 None。
    “她状态怎么样”这种后面直接跟别的字的不算命令。"""
    t = (text or "").strip()
    has_prefix = bool(t) and t[0] in PREFIX_CHARS
    t = t.lstrip(PREFIX_CHARS + " \t\u3000")
    for name in COMMANDS:
        if t.startswith(name):
            rest = t[len(name):]
            if rest == "" or rest[0] in " \t\u3000" or rest[0].isdigit():
                return name, rest.strip(), has_prefix
    return None


class _ForwardHandler(logging.Handler):
    """把 core/ 里用标准库 logging 写的日志转发到 AstrBot 的 logger，否则它们在 AstrBot 日志里看不到
    （例如“LLM 调用失败，使用兜底回复”——她只回“嗯嗯”时，原因就写在这里）。"""

    _gc_forward = True           # 用属性标记而不是 isinstance：插件重载后类对象会变，旧实例用 isinstance 认不出来
    _busy = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._busy:               # 防止 AstrBot 的 logger 又把日志送回这里造成递归
            return
        self._busy = True
        try:
            msg = f"{TAG} {record.getMessage()}"
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            if record.levelno >= logging.ERROR:
                logger.error(msg)
            elif record.levelno >= logging.WARNING:
                logger.warning(msg)
            elif record.levelno >= logging.INFO:
                logger.info(msg)
            else:
                logger.debug(msg)
        except Exception:                     # 日志本身永远不能影响对话
            pass
        finally:
            self._busy = False


def _bridge_logging() -> None:
    lg = logging.getLogger(PLUGIN_NAME)
    for h in [h for h in lg.handlers if getattr(h, "_gc_forward", False)]:   # 插件重载时先清掉旧的，避免重复转发
        lg.removeHandler(h)
    lg.addHandler(_ForwardHandler())
    lg.setLevel(logging.INFO)
    lg.propagate = False


# 模块导入成功就会打这一行：日志里有它 = 文件被 AstrBot 找到并成功导入；没有 = 插件没被加载或导入时就报错了
logger.info(f"{TAG} 模块导入成功 v{PLUGIN_VERSION}")


@register(PLUGIN_NAME, "Companion Contributors", "拟人化成长陪伴 4.0：表达节制、自然形态与云端语音", PLUGIN_VERSION)
class GrowingCompanion(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        _bridge_logging()
        self.config: dict[str, Any] = config if config is not None else {}
        self.data_dir = self._resolve_data_dir()
        self.engine = CompanionEngine(str(self.data_dir / "companion.db"), dict(self.config))
        fathers = [str(x) for x in (self.config.get("father_ids") or [])]
        logger.info(f"{TAG} 初始化完成 v{PLUGIN_VERSION}｜数据目录={self.data_dir}｜"
                    f"阶段={self.engine.growth.state()['stage']}｜"
                    f"父亲ID={'已配置%d个' % len(fathers) if fathers else '未配置（不会自动认领父亲，请在后台配置平台实例ID:用户ID）'}")
        self.stt = AstrBotSTT(context)
        self.tts = build_tts(context, self.config, self.data_dir / 'tts')
        self.sender, self.gate = ReplySender(), SessionGate()
        self.brain = BrainMapServer(
            str(self.data_dir / "companion.db"), host="0.0.0.0",
            port=int(self.config.get("brainmap_port", 5208)), access_key=self._brain_key(),
            max_nodes=int(self.config.get("brainmap_max_nodes", 300)),
            decay_speed=float(self.config.get("memory_decay_speed", 1.0)),
            fallback_tries=int(self.config.get("brainmap_port_fallback_tries", 0)), clock=self.engine.clock)
        self._bg: list[asyncio.Task] = []
        self._started = False
        # 密钥只在生命周期构造阶段创建。查看命令绝不触发初始化或写库。
        if not self.brain.access_key:
            self.brain.access_key = secrets.token_urlsafe(24)
            self.engine.db.kv_set('brainmap_key', self.brain.access_key)

    # ------------------------------------------------------------------ 初始化 / 生命周期
    def _resolve_data_dir(self) -> Path:
        try:
            p = Path(StarTools.get_data_dir(PLUGIN_NAME)) if StarTools else None
        except Exception:
            p = None
        p = p or Path("data") / "plugin_data" / PLUGIN_NAME
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _brain_key(self) -> str:
        key = str(self.config.get("brainmap_access_key", "") or "").strip()
        if key:
            return key
        key = self.engine.db.kv_get("brainmap_key") if hasattr(self, "engine") else None
        return key or ""

    async def initialize(self) -> None:
        await self._ensure_started()

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        if self.config.get("brainmap_enabled", True):
            if await self.brain.start():
                logger.info(f"{TAG} 脑图只读服务已启动：0.0.0.0:{self.brain.bound_port}")
            else:
                logger.error(f"{TAG} {self.brain.last_error}")
        else:
            logger.info(f"{TAG} 脑图服务未启用（配置里关闭了）")
        self._bg.append(asyncio.create_task(self._tick_loop()))
        logger.info(f"{TAG} 已就绪：命令 /她状态 等可用，私聊（或群里@）她会由插件接管回复")

    async def _tick_loop(self) -> None:
        """后台生活流：间隔带抖动，只更新内部状态，不强制任何行为。"""
        base = float(self.config.get("tick_interval_seconds", 480))
        while True:
            try:
                await asyncio.sleep(LifeTick.next_interval(base, self.engine.rng))
                await self.engine.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("生活流 tick 出错（已忽略）")

    async def terminate(self) -> None:
        for t in self._bg:
            t.cancel()
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)
        await self.brain.stop()
        for t in list(self.engine._tasks):
            t.cancel()
        await self.engine.wait_background()
        self.engine.close()

    # ------------------------------------------------------------------ 对话入口
    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    def _should_respond(self, event: AstrMessageEvent) -> bool:
        if self._is_private(event):
            return True
        return bool(getattr(event, "is_at_or_wake_command", False))   # 群聊：被@或唤醒才回

    @staticmethod
    def _actor(event: AstrMessageEvent) -> str:
        """用平台实例命名空间隔离身份；无平台元数据的单元测试桩可使用原始 ID。"""
        platform = getattr(getattr(event, 'platform_meta', None), 'id', '')
        uid = str(event.get_sender_id())
        return f'{platform}:{uid}' if platform else uid

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 不学习自身消息，避免平台回显造成自我回复循环。
        if hasattr(event, 'get_self_id') and str(event.get_sender_id()) == str(event.get_self_id()):
            return
        text = (event.message_str or "").strip()
        wake = self._should_respond(event)
        if wake and self.config.get("debug_log", True):      # 诊断日志：只记元信息和第一个字符，不记聊天内容
            logger.info(f"{TAG} 收到消息｜{'私聊' if self._is_private(event) else '群聊'}｜"
                        f"长度{len(text)}｜首字符={text[:1]!r}｜发送者{'是' if self._allowed(event) else '不是'}父亲")
        # 命令：不依赖 AstrBot 的唤醒前缀/命令过滤器，自己按名字识别（/她状态、她状态 都行）
        cmd = parse_command(text)
        if cmd and (cmd[2] or self._allowed(event)):
            async for r in self._dispatch(event, cmd[0], cmd[1]):
                yield r
            event.stop_event()
            return
        if text.startswith(tuple(PREFIX_CHARS)):              # 其他以 / 开头的（如 AstrBot 自带指令）放行
            return
        if hasattr(event, 'get_extra') and event.get_extra('handlers_parsed_params'):
            return  # 框架已识别的其他指令：即使前缀被剥掉也不观察/抢答。
        await self._ensure_started()

        is_voice = False
        if not text and self.config.get("voice_input_enabled", False):     # 语音输入：STT → 文本 → 正常流程
            for comp in event.get_messages():
                if isinstance(comp, Comp.Record):
                    heard = await self.stt.transcribe(comp.file or comp.url or "")
                    if heard:
                        text, is_voice = heard, True
                    break
        if not text:
            return

        sender = self._actor(event)
        is_group = not self._is_private(event)
        scope = event.unified_msg_origin
        # 潜水仍允许其他插件观察，但禁止宿主默认 LLM 替她冒泡。
        event.should_call_llm(False)
        # 同会话整个链路串行，第二轮规划能看到上一轮实际发送过的表达。
        async with self.gate.hold(scope):
            llm = AstrBotLLM(self.context, scope, str(self.config.get('light_provider_id', '')))
            reply = None
            delivered: list[str] = []
            try:
                reply = await self.engine.handle_message(sender, text, is_voice=is_voice, llm=llm,
                    scope_id=scope, is_group=is_group, directed=wake,
                    message_id=str(getattr(getattr(event, 'message_obj', None), 'message_id', '') or ''))
                if reply.silent:
                    if wake:
                        event.stop_event()  # 防止默认 LLM 替沉默/冷却中的她回答。
                    return
                # 直接 await event.send，不使用先 stop 再 yield（会被框架丢弃）。
                event.stop_event()
                p = reply.plan
                logger.info(f"{TAG} 已接管回复｜语言档={p.language_tier}｜意图={p.intent}｜"
                            f"形态={p.reply_mode}｜分条={len(reply.parts)}｜漂移分={p.persona_drift_score:.2f}")
                if self.engine.is_father(sender) and not is_group:
                    self.engine.db.kv_set('father_umo', scope)
                if self.config.get('voice_reply_policy', 'natural') == 'always' and not is_group:
                    reply.prefer_voice = True

                async def send_text(content: str):
                    return await event.send(event.plain_result(content))

                async def send_voice(path: str):
                    return await event.send(event.chain_result([Comp.Record(file=path, url=path)]))

                await self.sender.send(reply, send_text=send_text, send_voice=send_voice, delivered=delivered,
                    tts=self.tts, voice_enabled=bool(self.config.get('voice_output_enabled', False)))
            except Exception:
                logger.exception('陪伴链路失败')
                if wake:
                    event.stop_event()
            finally:
                if reply and reply.speech_token:
                    await self.engine.complete_reply(reply, delivered)

    # ------------------------------------------------------------------ 只读查看命令
    def _allowed(self, event: AstrMessageEvent) -> bool:
        return self.engine.is_father(self._actor(event))

    def _render(self, fn: Any, *args: Any) -> str:
        """统一的只读渲染：只读连接 + 异常兜底（数据不存在也给友好提示，不报错）。"""
        ro = None
        try:
            ro = self.engine.open_readonly()
            return fn(ro, self.engine.clock, self.engine.cfg, *args)
        except Exception:
            logger.exception("查看面板渲染失败")
            return "她现在还没有可以看的内容～"
        finally:
            if ro:
                ro.close()

    async def _dispatch(self, event: AstrMessageEvent, name: str, arg: str):
        """所有只读命令的唯一实现。on_message 和 @filter.command 两个入口都走这里；
        同一个事件只处理一次（谁先到谁处理），所以不会回两遍。"""
        if getattr(event, "_gc_cmd_done", False):
            return
        try:
            event._gc_cmd_done = True
        except Exception:
            pass
        if not self._is_private(event):
            event.stop_event()
            return  # 面板和密钥只在父亲私聊展示，群里也不刷“请私聊”。
        logger.info(f"{TAG} 命令「{name}」由插件处理")
        if not self._allowed(event):
            yield event.plain_result(DENY)
            event.stop_event()
            return
        try:
            if name == "她状态":
                yield event.plain_result(self._render(views.render_status))
            elif name == "记忆列表":
                yield event.plain_result(self._render(views.render_memory_list, int(arg) if arg.isdigit() else 1))
            elif name == "记忆详情":
                if arg.isdigit() and int(arg) > 0:
                    yield event.plain_result(self._render(views.render_memory_detail, int(arg)))
                else:
                    yield event.plain_result("用法：/记忆详情 编号（编号见 /记忆列表 里的 #数字）")
            elif name == "成长报告":
                yield event.plain_result(self._render(views.render_growth_report))
            elif name == "关系状态":
                yield event.plain_result(self._render(views.render_relationship))
            elif name == "最近复盘":
                yield event.plain_result(self._render(views.render_last_reflection))
            elif name == "脑图":
                async for r in self._brainmap(event, arg):
                    yield r
        except Exception as e:                  # 出错也要让爸爸在聊天里看到，而不是掉进默认人格
            logger.exception(f"{TAG} 命令「{name}」执行出错")
            yield event.plain_result(f"这个命令出错了（{type(e).__name__}），详情在日志里搜 growing_companion。")
        finally:
            event.stop_event()  # 最后一个 yield 已交给发送阶段，随后阻止默认模型重复回复。

    async def _brainmap(self, event: AstrMessageEvent, mode: str):
        if mode.strip() in ("文件", "file", "html") or not self.brain.running:
            ro = self.engine.open_readonly()
            try:
                graph = export_graph(ro, self.engine.clock.now(), max_nodes=int(self.config.get("brainmap_max_nodes", 300)),
                                     decay_speed=float(self.config.get("memory_decay_speed", 1.0)))
            finally:
                ro.close()
            path = self.data_dir / "brainmap.html"
            path.write_text(render_page(graph), encoding="utf-8")
            note = "" if self.brain.running else f"（在线服务没起来：{self.brain.last_error or '未启用'}）\n"
            yield event.plain_result(f"{note}给你一份自包含的脑图文件，用浏览器打开就能看（只读快照）。")
            yield event.chain_result([Comp.File(name="brainmap.html", file=str(path))])
            return
        base = str(self.config.get("brainmap_public_base_url", "") or "").rstrip("/")
        port = self.brain.bound_port
        url = f"{base}/?key={self.brain.access_key}" if base else f"http://<你的服务器地址>:{port}/?key={self.brain.access_key}"
        yield event.plain_result(
            f"脑图服务运行中（只读，监听 0.0.0.0:{port}）。\n链接：{url}\n"
            "云平台（如 instabox）里把这个端口发布出来，会得到公网 HTTPS 地址，在后面加上 /?key=… 即可。\n"
            "换服务器只需放行该端口，或在插件配置里改“脑图服务端口”。")

    # 下面是 AstrBot 标准的命令注册（让命令出现在指令列表里）；真正的实现都在 _dispatch
    @filter.command("她状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看她此刻的情绪、身体状态、时间感、注意力、成长阶段（只读）"""
        async for r in self._dispatch(event, "她状态", ""):
            yield r

    @filter.command("记忆列表")
    async def cmd_memory_list(self, event: AstrMessageEvent, page: int = 1):
        """按权重排序的记忆列表（只读）"""
        async for r in self._dispatch(event, "记忆列表", str(page)):
            yield r

    @filter.command("记忆详情")
    async def cmd_memory_detail(self, event: AstrMessageEvent, n: int = 0):
        """查看某条记忆的原文、权重、情绪坐标、召回次数等（只读）；n 为记忆列表里的编号"""
        async for r in self._dispatch(event, "记忆详情", str(n)):
            yield r

    @filter.command("成长报告")
    async def cmd_growth(self, event: AstrMessageEvent):
        """能力池、熟练度、练习次数与成长里程碑（只读）"""
        async for r in self._dispatch(event, "成长报告", ""):
            yield r

    @filter.command("关系状态")
    async def cmd_relationship(self, event: AstrMessageEvent):
        """她对爸爸的信任/依恋、共同记忆与最近关系变化（只读）"""
        async for r in self._dispatch(event, "关系状态", ""):
            yield r

    @filter.command("最近复盘")
    async def cmd_reflection(self, event: AstrMessageEvent):
        """她最近一次自己复盘想通了什么（只读）"""
        async for r in self._dispatch(event, "最近复盘", ""):
            yield r

    @filter.command("脑图")
    async def cmd_brainmap(self, event: AstrMessageEvent, mode: str = ""):
        """3D 记忆网络（只读）。/脑图 给链接；/脑图 文件 发送自包含 HTML 兜底"""
        async for r in self._dispatch(event, "脑图", mode):
            yield r
