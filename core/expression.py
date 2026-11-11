"""表达形态与投递：纯文本整形和异步发送分开，便于验证沉默与部分发送失败。"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from .models import BehaviorPlan, EngineReply
from .speech import TTSProvider

log = logging.getLogger('growing_companion.expression')


def shape_reply(text: str, plan: BehaviorPlan, *, max_delay: float = 8, delay_enabled: bool = True) -> list[str]:
    if plan.reply_mode == 'silence' or plan.silent:
        plan.silent, plan.reply_mode, plan.split_count = True, 'silence', 0
        plan.delay_seconds = plan.reply_delay_s = 0
        return []
    limit = min(plan.max_chars, 24) if plan.is_group else plan.max_chars
    if plan.reply_mode == 'short':
        limit = min(limit, 8)
    text = text.strip()[:max(1, limit)].rstrip('，, \n')
    if plan.truncate_style != 'none' and not plan.safety and len(text) > 6:
        ending = '…' if plan.truncate_style == 'trail_off' or limit < 12 else '…算了。'
        text = text[:max(1, min(len(text) * 2 // 3, limit - len(ending)))].rstrip('。！？!?，, ') + ending
    if not text:
        text = '嗯。'[:max(1, limit)]
    count = min(3, max(1, plan.split_count)) if plan.reply_mode == 'multi' and not plan.is_group else 1
    units = [s.strip() for s in re.findall(r'[^。！？!?\n]+[。！？!?]*', text) if s.strip()]
    # 只有一个念头就只发一条；不靠填充“对了”“还有”凑条数。
    count = min(count, len(units))
    if count <= 1:
        parts = [text.replace('\n', ' ')]
    else:
        parts = []
        for index, unit in enumerate(units):
            slot = min(count - 1, index * count // len(units))
            if len(parts) <= slot:
                parts.append(unit)
            else:
                parts[slot] += unit
    plan.split_count = len(parts)
    if plan.reply_mode == 'multi' and len(parts) == 1:
        plan.reply_mode = 'single'
    # 按实际第一条长度计算打字时间，而非生成前猜测；后续每条也有独立间隔。
    plan.delay_seconds = min(max(0, max_delay), max(.3, plan.delay_seconds + len(parts[0]) / 9)) if delay_enabled else 0
    plan.reply_delay_s = plan.delay_seconds
    return parts


class SessionGate:
    """同一会话的规划→发送→确认串行；不同会话可在发送等待期间并行。"""
    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, scope: str):
        lock, users = self._locks.get(scope, (asyncio.Lock(), 0))
        self._locks[scope] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[scope]
            if users == 1:
                del self._locks[scope]
            else:
                self._locks[scope] = (lock, users - 1)


class ReplySender:
    def __init__(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.sleep = sleep

    async def send(self, reply: EngineReply, *, send_text: Callable[[str], Awaitable[object]],
                   send_voice: Callable[[str], Awaitable[object]], delivered: list[str],
                   tts: TTSProvider | None = None, voice_enabled: bool = False) -> None:
        """delivered 逐条填充；异常/取消后，调用方仍可在 finally 中确认已送达部分。"""
        if reply.silent or reply.plan.reply_mode == 'silence':
            return
        parts = reply.parts or [reply.text]
        if reply.plan.is_group:
            parts = parts[:1]
        for index, part in enumerate(parts):
            delay = reply.delay_s if index == 0 else min(3.5, .35 + len(part) / 12) if reply.delay_s > 0 else 0
            if delay:
                await self.sleep(delay)
            path = None
            if voice_enabled and reply.prefer_voice and not reply.plan.is_group and tts:
                try:
                    style_synth = getattr(tts, 'synthesize_style', None)
                    operation = style_synth(part, reply.plan.tone) if style_synth else tts.synthesize(part)
                    path = await asyncio.wait_for(operation, timeout=95)
                except Exception as exc:
                    log.warning('语音合成失败，改发文字：%s', type(exc).__name__)
            try:
                result = await asyncio.wait_for(send_voice(path) if path else send_text(part), timeout=30)
                if result is False:
                    raise RuntimeError('平台明确返回发送失败')
                delivered.append(part)
            except Exception as exc:
                # 平台异常可能发生在已受理之后，不能擅自重发或改发文字造成重复。
                log.warning('消息发送未确认，停止后续分条：%s', type(exc).__name__)
                break
