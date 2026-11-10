"""记忆（衰减/强化/关联唤醒/元记忆/重构）与情绪（惯性/后悔/嘴硬心软）。"""
from growing_companion.core.emotion import EmotionEngine
from growing_companion.core.memory import clarity_of, effective_weight, render_memory
from growing_companion.core.models import EmotionSnapshot

from .conftest import DAY, run


def _strong(dom="sadness"):
    v = {"joy": 0.1, "trust": 0.4, "fear": 0.3, "sadness": 0.1, "curiosity": 0.1, "anger": 0.1}
    v[dom] = 0.85
    return EmotionSnapshot(values=v, dominant=dom, intensity=0.8, valence=-0.6, arousal=0.7)


def test_decay_reinforce_and_emotion_weighting(env):
    mem = env.engine.memory
    plain = mem.add(mtype="episodic", content="爸爸：今天看到一朵云", now=env.t, importance=0.4)
    recalled = mem.add(mtype="episodic", content="爸爸：今天看到一只猫", now=env.t, importance=0.4)
    emo = mem.add(mtype="episodic", content="爸爸：我走丢了好害怕", now=env.t, importance=0.4, snap=_strong("fear"))
    later = env.t + 60 * DAY
    for _ in range(6):
        mem.reinforce([type("R", (), {"memory": mem.get(recalled)})()], env.t + 10 * DAY)
    w = {k: effective_weight(mem.get(i), later) for k, i in [("plain", plain), ("recalled", recalled), ("emo", emo)]}
    assert w["plain"] < effective_weight(mem.get(plain), env.t)            # 会衰减
    assert w["recalled"] > w["plain"]                                      # 被召回的衰减更慢
    assert w["emo"] > w["plain"]                                           # 情绪强的衰减更慢
    assert mem.get(emo)["protected"] == 1                                  # 强情绪自动受保护


def test_association_wakes_dormant_memory_and_meta_memory(env):
    mem = env.engine.memory
    old = mem.add(mtype="episodic", content="爸爸：外婆家院子里有一棵桂花树", now=env.t, importance=0.3, tags=["k:桂花"])
    env.advance(400 * DAY)
    mem.refresh_weights(env.t)
    row = mem.get(old)
    assert row["dormant"] == 1                                             # 太久没想起：沉睡
    rec = mem.retrieve("你还记得外婆家的桂花树吗", ["桂花", "外婆"], env.t, 5)
    hit = [r for r in rec if r.memory["id"] == old]
    assert hit and hit[0].via in ("awakened", "direct")                    # 词面线索把它唤醒，而不是被删掉
    assert hit[0].vague and "记不太清了" in hit[0].text                      # 元记忆：知道自己记不清
    assert clarity_of(mem.get(old), env.t) < 0.4


def test_linked_memory_surfaces_via_association(env):
    mem = env.engine.memory
    a = mem.add(mtype="episodic", content="爸爸：我们一起在公园放了风筝", now=env.t, importance=0.5, tags=["k:风筝"])
    b = mem.add(mtype="episodic", content="爸爸：那天回家路上买了冰淇淋", now=env.t + 60, importance=0.5, tags=["k:冰淇淋"])
    mem.link(a, b, "temporal", 0.9)
    rec = mem.retrieve("还记得放风筝吗", ["风筝"], env.t + 120, 5)
    ids = {r.memory["id"]: r.via for r in rec}
    assert a in ids and b in ids and ids[b] in ("assoc", "awakened", "direct")   # 没提冰淇淋，也被关联想起


def test_reconstruct_keeps_original(env):
    mem = env.engine.memory
    mid = mem.add(mtype="episodic", content="爸爸：我骂你了，你别哭", now=env.t, importance=0.5)
    assert mem.reconstruct(mid, "爸爸那次其实是太累了，不是真的凶我", "复盘后新的理解", env.t + 100)
    m = mem.get(mid)
    assert m["content"] == "爸爸：我骂你了，你别哭" and "太累" in m["gist"] and m["reconstruct_count"] == 1
    assert env.engine.db.query_one("SELECT COUNT(*) n FROM memory_revisions WHERE memory_id=?", (mid,))["n"] == 1


def test_spontaneous_surfacing_prefers_unresolved_emotional(env):
    mem = env.engine.memory
    mid = mem.add(mtype="relational", content="爸爸：我凶了你，对不起还没说", now=env.t, snap=_strong("anger"),
                  importance=0.9, unresolved=True)
    env.advance(DAY)
    hits = 0
    import random
    for i in range(200):
        r = mem.surface(env.t + i * 7 * 3600, None, random.Random(i), energy=0.9, cooldown_s=1)
        hits += bool(r and r.memory["id"] == mid)
    assert 0 < hits < 200                                                  # 会冒出来，但不是每次，也不是从不


def test_emotion_inertia_regret_and_tsundere(env):
    emo = env.engine.emotion
    traits = env.engine.persona.traits()
    st = emo.load()
    st = emo.apply(st, {"sadness": 0.6, "anger": 0.6, "joy": 0.6}, 0.8, 0.8, env.t)
    snap = EmotionEngine.snapshot(st, traits, 0.8)
    assert snap.tsundere and snap.mixed                                     # 又气又难过：嘴硬心软
    later = emo.project(st, env.t + 3 * 3600)
    assert later.values["joy"] < later.values["sadness"] + 0.5
    assert (st.values["sadness"] - later.values["sadness"]) < (st.values["joy"] - later.values["joy"]) + 0.2   # 难过比开心惯性大
    cooled = emo.project(st, env.t + 5 * 3600)
    assert cooled.regret > 0.2                                              # 气消了以后会后悔
    assert emo.project(cooled, env.t + 40 * 3600).regret < cooled.regret   # 后悔也会淡
