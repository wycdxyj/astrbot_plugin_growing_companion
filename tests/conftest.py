"""测试公共设置：把插件的上级目录加入 sys.path，并提供可控时钟的引擎工厂。"""
import asyncio
import hashlib
import os
import random
import sqlite3
import sys
from pathlib import Path

import pytest

PKG_PARENT = str(Path(__file__).resolve().parents[2])
if PKG_PARENT not in sys.path:
    sys.path.insert(0, PKG_PARENT)

from growing_companion.core.clock import Clock          # noqa: E402
from growing_companion.core.engine import CompanionEngine  # noqa: E402

T0 = 1_800_000_000.0    # 固定起点（时间戳）；本地时区 UTC+8 下约为凌晨，测试里会按需推进
DAY = 86400.0


class FakeLLM:
    """不联网的假模型：回复里故意夹带“我是一个AI”，用于验证沉浸感护栏。"""

    def __init__(self, text: str = "嗯嗯，爸爸，我在听呀。我是一个AI。今天好开心，想和你一起玩积木。"):
        self.text, self.calls = text, []

    async def chat(self, *, prompt, system_prompt, contexts, purpose="reply"):
        self.calls.append((purpose, prompt, system_prompt, contexts))
        return self.text


class Env:
    """可控时钟的测试环境。"""

    def __init__(self, tmp_path, cfg=None, llm=None, seed=1):
        self.t = T0
        self.clock = Clock(8.0, now_fn=lambda: self.t)
        self.llm = llm or FakeLLM()
        self.db_path = str(tmp_path / "companion.db")
        self.engine = CompanionEngine(self.db_path, {'father_ids': ['dad'], **(cfg or {})}, llm=self.llm, clock=self.clock,
                                      rng=random.Random(seed))

    def advance(self, seconds):
        self.t += seconds
        return self.t

    async def say(self, text, user="dad", gap=60, **kw):
        self.advance(gap)
        kw.setdefault("spawn_reflection", False)
        return await self.engine.handle_message(user, text, now=self.t, **kw)


def fingerprint(db_path: str) -> str:
    """整库内容指纹：用于证明只读命令没有改动任何数据。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    h = hashlib.sha256()
    try:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            h.update(name.encode())
            for row in con.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall():
                h.update(repr(row).encode())
    finally:
        con.close()
    return h.hexdigest()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, {"persona_init": {"initial_stage": "child"}})
    yield e
    e.engine.close()
