"""上下文预算测试：长度上限、压缩前后注入对比、长对话预算稳定性。"""
import random

from growing_companion.core.budget import estimate_tokens
from growing_companion.core.memory import effective_weight
from growing_companion.core.prompts import build_retrieval_segments

from .conftest import DAY, Env, run


def _inject_tokens(env, query, k):
    """模拟“把相关记忆注入上下文”需要多少 token（压缩前后对比用）。"""
    rec = env.engine.memory.retrieve(query, [query], env.t, k)
    segs = build_retrieval_segments(rec, [], None, None)
    return sum(estimate_tokens(t) + 1 for t, _ in segs), rec


def test_context_length_within_budget(tmp_path):
    env = Env(tmp_path, {"persona_init": {"initial_stage": "child"}, "token_budget_total": 3000})
    try:
        for i in range(120):          # 预置大量记忆，确认不会被全量塞入
            env.engine.memory.add(mtype="episodic", content=f"爸爸：今天聊到了第{i}件关于公园和积木的事，说了很多很多话" * 2,
                                  now=env.t - i * 3600, importance=0.5, tags=["k:公园", "k:积木"])
        r = run(env.say("爸爸今天带你去公园玩积木好不好呀"))
        u = r.usage
        assert u.total <= 3000
        assert u.fixed <= env.engine.budget.fixed_max
        assert u.state <= env.engine.budget.state_max
        assert u.retrieval <= env.engine.budget.retrieval_max
        sys_prompt = env.llm.calls[-1][2]
        assert sys_prompt.count("\n· [") + sys_prompt.count("\n· ") < 40      # 远少于 120 条
        assert sys_prompt.count("[") < 25
    finally:
        env.engine.close()


def test_trivial_message_does_not_fill_budget(env):
    r = run(env.say("嗯"))
    assert r.usage.total < env.engine.budget.total * 0.5        # 上限不是目标


def test_fixed_layer_is_cached(env):
    r1 = run(env.say("你好呀宝贝"))
    r2 = run(env.say("今天天气不错"))
    assert r1.usage.fixed_cache_hit is False
    assert r2.usage.fixed_cache_hit is True
    assert env.engine.budget.cache_hits >= 1


def test_compression_reduces_injection_and_keeps_originals(env):
    mem = env.engine.memory
    ids = []
    for i in range(40):               # 40 条重复的低情绪琐碎日常
        ids.append(mem.add(mtype="episodic", content=f"爸爸：今天中午吃了米饭和青菜，第{i}次\n我：好吃好吃",
                           now=env.t, importance=0.25, tags=["k:米饭"]))
    # 1 条强情绪的、必须保留原始的记忆
    from growing_companion.core.models import EmotionSnapshot
    strong = EmotionSnapshot(values={"joy": 0.1, "trust": 0.4, "fear": 0.7, "sadness": 0.8, "curiosity": 0.1, "anger": 0.1},
                             dominant="sadness", intensity=0.8, valence=-0.8, arousal=0.7)
    protected_id = mem.add(mtype="relational", content="爸爸：今天吃米饭的时候你凶了我，我哭了", now=env.t,
                           snap=strong, importance=0.8, tags=["k:米饭"])
    env.advance(DAY)
    before, rec_before = _inject_tokens(env, "米饭", 60)
    n_before = env.engine.db.query_one("SELECT COUNT(*) n FROM memories")["n"]

    groups = mem.find_compressible(env.t, min_group=3, max_group=14)
    assert groups and all(protected_id not in g for g in groups)          # 强情绪记忆绝不入选
    for g in groups:
        rows = [mem.get(i) for i in g]
        assert mem.compress_group(g, mem.extractive_summary(rows), env.t)

    after, rec_after = _inject_tokens(env, "米饭", 60)
    n_after = env.engine.db.query_one("SELECT COUNT(*) n FROM memories")["n"]
    assert after < before * 0.5, (before, after)                           # 压“量”
    assert n_after == n_before + len(groups)                              # 原始一条没少，只多了语义记忆
    p = mem.get(protected_id)
    assert p["compressed"] == 0 and p["protected"] == 1                  # 压量不压情：强情绪保留原始
    assert protected_id in [r.memory["id"] for r in rec_after]            # 且仍会被想起
    comp = [mem.get(i) for i in ids if mem.get(i)["compressed"]]
    assert comp and all(c["compressed_into"] for c in comp)
    sem = mem.get(comp[0]["compressed_into"])
    assert sem["type"] == "semantic" and comp[0]["id"] in sem["source_ids"]  # 原始记忆引用


def test_long_conversation_budget_is_stable(tmp_path):
    env = Env(tmp_path, {"persona_init": {"initial_stage": "child"}, "token_budget_total": 3000}, seed=7)
    try:
        rnd = random.Random(3)
        topics = ["公园", "积木", "小猫", "晚饭", "画画", "下雨", "故事书", "草莓蛋糕", "幼儿园", "小熊"]
        feel = ["你真乖", "我有点累了", "不许再这样了", "对不起我错了", "哈哈太好笑了", "为什么天会黑呢", "晚安"]
        totals = []

        async def go():
            for i in range(150):
                msg = f"{rnd.choice(topics)}{rnd.choice(topics)}，{rnd.choice(feel)}，今天第{i}次聊到{rnd.choice(topics)}"
                r = await env.say(msg, gap=rnd.choice([60, 300, 3600]), spawn_reflection=True)
                totals.append(r.usage.total)
                if i % 25 == 0:
                    await env.engine.wait_background()
            await env.engine.wait_background()
        run(go())
        assert max(totals) <= 3000                                   # 任何一轮都不超预算
        warm = sum(totals[50:100]) / 50
        late = sum(totals[100:]) / 50
        assert late <= warm * 1.25 + 60, (warm, late)                # 不会随对话变长而膨胀
        assert env.engine.db.query_one("SELECT COUNT(*) n FROM token_usage")["n"] > 0
    finally:
        env.engine.close()
