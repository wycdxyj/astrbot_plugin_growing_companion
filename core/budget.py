"""上下文预算管理器：分层注入、top-k、压缩后注入、滑动窗口、固定层缓存、用量监控。

总预算 3000~5000 token 是“上限不是目标”：状态层只放当前有效状态、检索层只放相关度够的 top-k，
日常闲聊用不满；超出时按“先丢旧对话 → 再丢低分记忆 → 再丢次要状态 → 最后换精简固定层”的顺序收缩。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Callable

from .db import Database
from .models import AssembledContext, ContextUsage

Segment = tuple[str, float]
_CJK = re.compile(r"[一-鿿]")


def estimate_tokens(text: str) -> int:
    """UTF-8 字节上界，覆盖常见 byte-level tokenizer；不冒称供应商实测 token。"""
    return len(text.encode('utf-8'))


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_tokens = max(0, max_tokens)
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) + 3 <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ("…" if max_tokens >= 3 else '')


class ContextBudgetManager:
    def __init__(self, total: int = 4000, fixed_max: int = 1200, state_max: int = 400,
                 retrieval_max: int = 1200, dialog_max: int = 1600, window_turns: int = 12, topk: int = 6):
        self.total = max(3000, min(5000, total))
        self.fixed_max = max(1100, min(1600, int(fixed_max)))
        self.state_max = max(200, min(1200, int(state_max)))
        self.retrieval_max = max(0, min(self.total, int(retrieval_max)))
        self.dialog_max = max(128, min(self.total, int(dialog_max)))
        self.window_turns = max(1, window_turns)
        self.topk = max(0, topk)
        self._fixed_cache: "OrderedDict[str, str]" = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0

    # ---------------- 固定层缓存 ----------------
    def get_fixed(self, key: str, build: Callable[[], str]) -> tuple[str, bool]:
        if key in self._fixed_cache:
            self._fixed_cache.move_to_end(key)
            self.cache_hits += 1
            return self._fixed_cache[key], True
        text = build()
        self._fixed_cache[key] = text
        if len(self._fixed_cache) > 16:
            self._fixed_cache.popitem(last=False)
        self.cache_misses += 1
        return text, False

    # ---------------- 分层装配 ----------------
    @staticmethod
    def _fit(segs: list[Segment], budget: int, keep_order: bool = True) -> tuple[list[Segment], int]:
        """按优先级挑选，直到预算用完；返回(入选片段, 被丢弃数)。keep_order 时保持原始顺序。"""
        order = sorted(range(len(segs)), key=lambda i: -segs[i][1])
        chosen: list[int] = []
        used = 0
        for i in order:
            t = estimate_tokens(segs[i][0]) + 1
            if used + t <= budget:
                chosen.append(i)
                used += t
        if keep_order:
            chosen.sort()
        return [segs[i] for i in chosen], len(segs) - len(chosen)

    def assemble(self, *, fixed_key: str, fixed_full: Callable[[], str], fixed_short: Callable[[], str],
                 state_segs: list[Segment], retrieval_segs: list[Segment],
                 dialog: list[dict[str, str]], user_text: str) -> AssembledContext:
        usage = ContextUsage(budget=self.total)
        fixed, hit = self.get_fixed(fixed_key, fixed_full)
        usage.fixed_cache_hit = hit
        if estimate_tokens(fixed) > self.fixed_max:
            fixed = fixed_short()
        fixed = _truncate_to_tokens(fixed, self.fixed_max)

        state, _ = self._fit(state_segs, self.state_max)
        retr, dropped = self._fit(retrieval_segs[: max(self.topk, 0) + 8], self.retrieval_max, keep_order=False)
        retr = retr[: self.topk + 4]     # 记忆 top-k 之外只允许少量信念/里程碑/复盘行
        usage.dropped_memories = dropped

        prompt = _truncate_to_tokens(user_text, 600)
        turns = self._window(dialog)
        p_tokens = estimate_tokens(prompt)

        def tally() -> int:
            return (estimate_tokens(fixed) + sum(estimate_tokens(t) + 1 for t, _ in state)
                    + sum(estimate_tokens(t) + 1 for t, _ in retr) + self._turn_tokens(turns) + p_tokens + 256)

        total = tally()
        # 超限收缩顺序：旧对话 → 低分记忆 → 次要状态 → 精简固定层
        while total > self.total and len(turns) > 2 and self._turn_tokens(turns) > 300:
            turns = turns[2:] if len(turns) > 2 else turns[1:]
            usage.trimmed_turns += 2
            total = tally()
        while total > self.total and len(retr) > 0 and sum(estimate_tokens(t) for t, _ in retr) > 200:
            retr = retr[:-1] if len(retr) > 1 else []
            usage.dropped_memories += 1
            total = tally()
        while total > self.total and len(state) > 2:
            low = min(range(len(state)), key=lambda i: state[i][1])
            state.pop(low)
            total = tally()
        if total > self.total:
            fixed = _truncate_to_tokens(fixed_short(), self.fixed_max)
            total = tally()
        while total > self.total and retr:
            retr = retr[:-1]
            total = tally()
        while total > self.total and len(turns) > 0:
            turns = turns[1:]
            usage.trimmed_turns += 1
            total = tally()
        while total > self.total and state:
            state.pop(min(range(len(state)), key=lambda i: state[i][1]))
            total = tally()

        system = fixed
        if state:
            system += "\n\n【本轮状态】\n" + "\n".join(t for t, _ in state)
        if retr:
            system += "\n\n【此刻脑子里浮现的事（可能不完整，记不清就说记不清）】\n" + "\n".join(t for t, _ in retr)
        usage.fixed = estimate_tokens(fixed)
        usage.state = sum(estimate_tokens(t) + 1 for t, _ in state)
        usage.retrieval = sum(estimate_tokens(t) + 1 for t, _ in retr)
        usage.dialog = self._turn_tokens(turns) + p_tokens
        # 按实际拼接后的文本重新计算，包含层标题、分隔符和消息封装余量。
        usage.total = estimate_tokens(system) + self._turn_tokens(turns) + p_tokens + 128
        if usage.total > self.total:
            raise ValueError('上下文超过保守预算；请缩短固定人格层')
        return AssembledContext(system, turns, prompt, usage)

    def _window(self, dialog: list[dict[str, str]]) -> list[dict[str, str]]:
        """滑动窗口：最近 N 轮，且不超过对话层预算；更早的交给记忆系统。"""
        recent = dialog[-self.window_turns * 2:]
        out: list[dict[str, str]] = []
        used = 0
        for m in reversed(recent):
            t = estimate_tokens(m["content"]) + 16
            if used + t > self.dialog_max:
                break
            out.append(m)
            used += t
        out.reverse()
        while out and out[0]["role"] != "user":
            out.pop(0)
        return out

    @staticmethod
    def _turn_tokens(turns: list[dict[str, str]]) -> int:
        return sum(estimate_tokens(m["content"]) + 16 for m in turns)


class UsageMonitor:
    """token 用量监控：写入 token_usage 表，供只读面板查看。"""

    def __init__(self, db: Database):
        self.db = db

    def record(self, u: ContextUsage, ts: float) -> None:
        self.db.execute("INSERT INTO token_usage(ts,fixed,state,retrieval,dialog,total,budget,fixed_cache_hit) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (ts, u.fixed, u.state, u.retrieval, u.dialog, u.total, u.budget, int(u.fixed_cache_hit)))
        self.db.execute("DELETE FROM token_usage WHERE id < (SELECT MAX(id) FROM token_usage) - 500")
