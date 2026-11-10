"""自主复盘：由内部状态驱动，没有固定频率；复盘后记忆被“消化”，冲动回落。"""
import asyncio
import random

from growing_companion.core.reflection import ReflectionDecision

from .conftest import DAY, FakeLLM, run


def test_reflection_pull_follows_experience_not_schedule(env):
    body = env.engine.body_model.load()
    calm_pull, _ = env.engine.reflection.compute_pull(env.t, body)
    assert calm_pull < 0.4                                                    # 什么都没发生：几乎不想复盘
    run(env.say("不许再这样了，烦死了！"))
    run(env.say("你真笨，闭嘴"))
    hot_pull, why = env.engine.reflection.compute_pull(env.t, env.engine.body_model.load())
    assert hot_pull > calm_pull and why                                       # 有情绪强的事 → 冲动上来，并说得出原因
    env.advance(30 * DAY)                                                     # 时间本身只提供很轻的压力
    env.engine.memory.mark_reviewed([r["id"] for r in env.engine.db.query("SELECT id FROM memories")], env.t)
    cold_pull, _ = env.engine.reflection.compute_pull(env.t, env.engine.body_model.load())
    assert cold_pull < hot_pull


def test_reflection_run_writes_long_term_memory_and_compresses(env):
    async def go():
        for i in range(8):
            await env.say(f"爸爸：今天中午吃了米饭和青菜，第{i}次", gap=60)
        env.advance(DAY)
        await env.say("不许再这样了，烦死了")
        env.advance(DAY)
        d = ReflectionDecision(True, 3, 0.9, 1.0, ["测试强制"])
        snap = env.engine.emotion.snapshot(env.engine.emotion.load(), env.engine.persona.traits())
        rid = await env.engine.reflection.run(d, env.t, llm=env.llm, style=env.engine.persona.style(),
                                              attachment=0.6, snap=snap, openness=0.5)
        return rid
    rid = run(go())
    db = env.engine.db
    row = db.query_one("SELECT * FROM reflections WHERE id=?", (rid,))
    assert row and row["summary"] and row["depth"] == 3
    assert db.query_one("SELECT COUNT(*) n FROM memories WHERE type='semantic'")["n"] >= 1      # 提炼出语义认知
    import json
    digested = json.loads(row["memory_ids_json"])
    assert digested and all(db.query_one("SELECT consolidation c FROM memories WHERE id=?", (i,))["c"] != "fresh"
                            for i in digested)                                                   # 这次想过的记忆都被消化了
    assert db.query_one("SELECT COUNT(*) n FROM memories WHERE content LIKE '%不许再这样%' AND compressed=0")["n"] >= 1


def test_reflection_is_async_and_never_blocks_or_breaks_reply(env):
    class Boom(FakeLLM):
        async def chat(self, *, prompt, system_prompt, contexts, purpose="reply"):
            if purpose == "reflection":
                raise RuntimeError("模型挂了")
            return await super().chat(prompt=prompt, system_prompt=system_prompt, contexts=contexts, purpose=purpose)
    env.engine.llm = Boom()

    async def go():
        for m in ["不许再这样了，烦死了", "你真笨", "闭嘴"]:
            r = await env.say(m, spawn_reflection=True)
            assert r.text                                                     # 回复不受复盘影响
        await env.engine.wait_background()                                    # 复盘失败也被吞掉，不崩
    run(go())
