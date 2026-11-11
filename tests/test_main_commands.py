"""用最小 AstrBot 桩模块加载 main.py：确认入口注册、命令只读、仅父亲可用。"""
import asyncio
import importlib
import sys
import types

from .conftest import fingerprint, run


def _install_stub():
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m
    astrbot, api, event, star, comps = (mod("astrbot"), mod("astrbot.api"), mod("astrbot.api.event"),
                                        mod("astrbot.api.star"), mod("astrbot.api.message_components"))
    astrbot.api = api
    import logging
    api.logger, api.AstrBotConfig = logging.getLogger("stub"), dict

    class _F:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def command(name):
            return lambda f: f

        @staticmethod
        def event_message_type(t):
            return lambda f: f

    event.filter = _F
    event.AstrMessageEvent = object
    star.Context, star.register = object, (lambda *a, **k: (lambda c: c))

    class Star:
        def __init__(self, context=None):
            self.context = context
    star.Star = Star
    for n in ("Record", "File", "Plain"):
        setattr(comps, n, type(n, (), {"__init__": lambda self, *a, **k: None}))
    api.message_components, api.event, api.star = comps, event, star


class FakeEvent:
    def __init__(self, uid):
        self.uid, self.message_str, self.unified_msg_origin = uid, "", "test:private:1"
        self.stopped = False
        self.sent = []
        self.call_llm = True

    def get_sender_id(self): return self.uid
    def plain_result(self, t): return t
    def chain_result(self, c): return c
    def stop_event(self): self.stopped = True
    def is_private_chat(self): return True
    def get_messages(self): return []
    async def send(self, message): self.sent.append(message)
    def should_call_llm(self, value): self.call_llm = value


def test_main_registers_and_readonly_commands(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)          # 必须在导入 main 之前：“模块导入成功”是导入时打的
    _install_stub()
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("growing_companion.main", None)
    main = importlib.import_module("growing_companion.main")
    plugin = main.GrowingCompanion(object(), {"father_ids": ["dad"], "brainmap_enabled": False,
                                              "persona_init": {"initial_stage": "child"}, "human_like_delay": False})

    async def drain(agen):
        return [x async for x in agen]

    async def go():
        ev = FakeEvent("dad")
        ev.message_str = "爸爸来啦，你真乖！"
        out = await drain(plugin.on_message(ev))               # 没有 LLM 供应商 → 走兜底回复，不崩
        assert out == [] and ev.sent and ev.stopped
        logs = caplog.text
        assert "[growing_companion] 模块导入成功" in logs and "[growing_companion] 初始化完成" in logs   # 启动日志
        assert "[growing_companion] 已接管回复" in logs and "语言档=" in logs                            # 接管日志
        assert "爸爸来啦" not in logs                                                                  # 不记聊天内容
        assert "LLM 调用失败" in logs                                                                  # core 里的 warning 也转发到了 AstrBot 日志
        fp = fingerprint(str(plugin.data_dir / "companion.db"))
        for cmd, args in [(plugin.cmd_status, ()), (plugin.cmd_memory_list, ()), (plugin.cmd_memory_detail, (1,)),
                          (plugin.cmd_growth, ()), (plugin.cmd_relationship, ()), (plugin.cmd_reflection, ())]:
            res = await drain(cmd(FakeEvent("dad"), *args))
            assert res and isinstance(res[0], str) and "Traceback" not in res[0]
        assert fingerprint(str(plugin.data_dir / "companion.db")) == fp           # 命令全程只读
        stranger = await drain(plugin.cmd_status(FakeEvent("someone")))
        assert stranger == [main.DENY]                                              # 非父亲不可看
        await plugin.terminate()
    run(go())
    sys.modules.pop("growing_companion.main", None)


def test_parse_command_variants():
    sys.modules.pop("growing_companion.main", None)
    _install_stub()
    main = importlib.import_module("growing_companion.main")
    p = main.parse_command
    assert p("/她状态") == ("她状态", "", True)
    assert p("她状态") == ("她状态", "", False)                      # AstrBot 可能已把唤醒前缀剥掉
    assert p("！记忆详情 12") == ("记忆详情", "12", True)
    assert p("/记忆详情12") == ("记忆详情", "12", True)
    assert p("/脑图 文件") == ("脑图", "文件", True)
    assert p("她状态怎么样") is None and p("今天画了脑图") is None     # 普通聊天里提到词，不算命令
    assert p("/help") is None and p("") is None
    sys.modules.pop("growing_companion.main", None)


def test_commands_work_with_or_without_prefix_and_never_double_reply(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    _install_stub()
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("growing_companion.main", None)
    main = importlib.import_module("growing_companion.main")
    plugin = main.GrowingCompanion(object(), {"father_ids": ["dad"], "brainmap_enabled": False,
                                              "persona_init": {"initial_stage": "child"}, "human_like_delay": False})

    async def drain(agen):
        return [x async for x in agen]

    async def go():
        for text in ("/她状态", "她状态", "！她状态"):                       # 有/无前缀都能识别（H1：唤醒前缀不是 / 的情况）
            ev = FakeEvent("dad"); ev.message_str = text
            out = await drain(plugin.on_message(ev))
            assert len(out) == 1 and "现在的状态" in out[0] and ev.stopped, text
        # 同一个事件：先 on_message 处理，再走 @filter.command 入口 → 不会回两遍
        ev = FakeEvent("dad"); ev.message_str = "/她状态"
        first = await drain(plugin.on_message(ev))
        second = await drain(plugin.cmd_status(ev))
        assert len(first) == 1 and second == []
        # 反过来：先 command 入口，再 on_message
        ev = FakeEvent("dad"); ev.message_str = "她状态"
        a = await drain(plugin.cmd_status(ev))
        b = await drain(plugin.on_message(ev))
        assert len(a) == 1 and b == []
        # 非父亲：带前缀 → 拒绝；不带前缀 → 当成普通聊天（不是命令，也不泄露面板）
        ev = FakeEvent("stranger"); ev.message_str = "/她状态"
        assert await drain(plugin.on_message(ev)) == [main.DENY]
        ev = FakeEvent("stranger"); ev.message_str = "她状态"
        out = await drain(plugin.on_message(ev))
        assert out == [] and ev.stopped  # 未配置模型的陌生人社交失败时沉默，不泄露面板。
        # 普通聊天里提到词 / AstrBot 自带指令：不当成命令
        ev = FakeEvent("dad"); ev.message_str = "她状态怎么样呀"
        out = await drain(plugin.on_message(ev))
        assert out == [] and ev.sent and "现在的状态" not in ev.sent[0]
        ev = FakeEvent("dad"); ev.message_str = "/help"
        assert await drain(plugin.on_message(ev)) == [] and not ev.stopped      # 放行给 AstrBot 自带指令
        await plugin.terminate()
    run(go())
    logs = caplog.text
    assert "收到消息｜私聊" in logs and "首字符='/'" in logs and "命令「她状态」由插件处理" in logs
    assert "她状态怎么样呀" not in logs                                       # 诊断日志不记聊天内容
    sys.modules.pop("growing_companion.main", None)


def test_log_forwarder_not_duplicated_on_reload():
    import logging
    _install_stub()
    sys.modules.pop("growing_companion.main", None)
    main = importlib.import_module("growing_companion.main")
    lg = logging.getLogger("growing_companion")
    main._bridge_logging()

    class OldVersionHandler(logging.Handler):          # 模拟“插件重载前、旧模块里的转发器”：类对象不同，只能靠属性认
        _gc_forward = True
        def emit(self, record): pass
    lg.addHandler(OldVersionHandler())
    main._bridge_logging()
    main._bridge_logging()
    assert len([h for h in lg.handlers if getattr(h, "_gc_forward", False)]) == 1
    sys.modules.pop("growing_companion.main", None)
