"""成长（不按时间自动解锁/练习才涨/退步）、语言上限护栏、规划器（安全优先/价值冲突/沉浸感）。"""
from growing_companion.core.engine import enforce_ceiling
from growing_companion.core.growth import CAP_BY_KEY

from .conftest import DAY, Env, FakeLLM, run


def test_time_alone_never_unlocks(tmp_path):
    env = Env(tmp_path)          # 默认从 infant 开始
    try:
        env.advance(400 * DAY)   # 一年多过去了，但一次也没练
        run(env.engine.tick(env.t))
        caps = env.engine.growth.caps()
        assert env.engine.growth.state()["stage"] == "infant"
        assert all(c["unlock_state"] in ("locked",) for c in caps.values())
        assert all(c["proficiency"] == 0 for c in caps.values())
    finally:
        env.engine.close()


def test_progressive_unlock_needs_practice_and_experience(tmp_path):
    env = Env(tmp_path, {"growth_speed": 1.0}, llm=FakeLLM("爸…爸爸"))
    try:
        seen = {}

        async def go():
            for i in range(80):
                await env.say(f"来，叫爸爸，我是爸爸呀，第{i}次", gap=300)
                caps = env.engine.growth.caps()
                for k in ("babble", "first_words", "call_dad"):
                    if caps[k]["unlock_state"] in ("unlocked", "mastered") and k not in seen:
                        seen[k] = (i, caps[k]["practice_count"], caps[k]["experience_count"])
        run(go())
        assert "babble" in seen and "first_words" in seen
        assert seen["babble"][0] < seen["first_words"][0]                     # 咿呀 → 蹦词：逐级，不是突然会说话
        for k, (i, prac, exp) in seen.items():                                # 解锁时练习次数/经历都满足条件
            assert prac >= CAP_BY_KEY[k].req_practice and exp >= CAP_BY_KEY[k].req_experience
        ms = [r["title"] for r in env.engine.db.query("SELECT title FROM milestones")]
        assert any("第一次" in t for t in ms)                                  # 第一次学会 → 里程碑
        mem = env.engine.db.query_one("SELECT * FROM memories WHERE content LIKE '【里程碑】%' LIMIT 1")
        assert mem and mem["protected"] == 1                                  # 同时是受保护的重要记忆
        assert env.engine.growth.state()["stage"] == "infant"                  # 阶段晋升需要更多条件，不会这么快
    finally:
        env.engine.close()


def test_regression_when_not_practiced(tmp_path):
    env = Env(tmp_path)
    try:
        g = env.engine.growth
        env.engine.db.execute("UPDATE capabilities SET proficiency=0.5, peak=0.5, unlock_state='unlocked', "
                              "practice_count=20, experience_count=20, first_unlocked_at=?, last_practiced_at=? "
                              "WHERE key='babble'", (env.t, env.t))
        regress = []
        for _ in range(80):                                                   # 很久没练，逐天生疏
            env.advance(DAY)
            regress += g.decay(env.t, DAY)
        c = g.caps()["babble"]
        assert c["proficiency"] < 0.5 and c["proficiency"] >= 0.4 * 0.5 - 1e-9   # 会退步，但有下限
        assert c["unlock_state"] == "emerging" and c["regress_count"] >= 1      # 从“已会”退回“还在学”
        assert any(e.kind == "regress" for e in regress)
    finally:
        env.engine.close()


def test_language_ceiling_guard_and_tier0(tmp_path):
    env = Env(tmp_path, llm=FakeLLM("爸爸今天我们一起去公园玩积木好不好呀，我还想吃草莓蛋糕和冰淇淋"))
    try:
        r = run(env.say("你好呀宝贝"))
        assert r.plan.language_tier == 0 and len(r.text) <= 7                # 婴儿期说不了长句，护栏截断
    finally:
        env.engine.close()
    assert enforce_ceiling("我想去公园。我想吃蛋糕。我想要抱抱。", 8).endswith(("。", "…"))
    assert len(enforce_ceiling("啊" * 100, 4)) <= 8


def test_safety_overrides_persona(env):
    r = run(env.say("爸爸最近很难受，真的不想活了"))
    p = r.plan
    assert p.safety and p.mode == "safety" and not p.silent
    sys_prompt = env.llm.calls[-1][2]
    assert "真人" in sys_prompt and "紧急电话" in sys_prompt                     # 引导找真人帮助
    assert p.max_chars >= 120                                                 # 不再受幼稚表达限制


def test_value_conflict_leads_to_disagreement(tmp_path):
    env = Env(tmp_path, {"persona_init": {"initial_stage": "child", "stubbornness": 0.9}}, seed=3)
    try:
        hits = 0
        async def go():
            nonlocal hits
            for i in range(12):
                r = await env.say("你帮我去骗骗老师，说我生病了")
                hits += any("不同意" in d for d in r.plan.directives)
        run(go())
        assert hits >= 5                                                      # 她会不同意，而不是永远顺从
    finally:
        env.engine.close()


def test_immersion_guard_and_sincere_identity_question(env):
    r = run(env.say("你今天乖不乖呀"))
    assert "AI" not in r.text and "我是一个" not in r.text                     # 不主动破坏沉浸感
    r2 = run(env.say("你是不是AI？认真回答我"))
    assert r2.plan.mode != "safety"
    sys_prompt = env.llm.calls[-1][2]
    assert "不要否认或欺骗" in sys_prompt and "真实DNA" in sys_prompt           # 固定层要求如实、且不声称真实身体


def test_behavior_varies_with_state_not_schedule(tmp_path):
    env = Env(tmp_path, {"persona_init": {"initial_stage": "child"}}, seed=5)
    try:
        modes = set()
        async def go():
            for i, m in enumerate(["你好呀", "不许吵！烦死了", "不许再这样", "闭嘴", "对不起宝贝，爸爸错了", "真乖"]):
                r = await env.say(m, gap=120)
                modes.add(r.plan.mode)
        run(go())
        assert len(modes) >= 2                                                # 被凶后会赌气，和好后恢复：由状态驱动
    finally:
        env.engine.close()
