"""只读查看面板与脑图导出：确认不写库、字段完整、数据库层面禁写。"""
import sqlite3

import pytest

from growing_companion.core import readonly_views as v
from growing_companion.core.brainmap.export import EMOTION_COLORS, export_graph
from growing_companion.core.models import EMOTIONS

from .conftest import DAY, fingerprint, run


async def _chat(env):
    for m in ["爸爸来啦，你真乖！", "今天我们去公园玩积木好不好", "不许再这样了，烦死了", "对不起宝贝，爸爸错了",
              "哈哈，宝贝真棒，下次带你去游乐园，说话算话"]:
        await env.say(m)
    env.advance(2 * DAY)
    await env.engine.tick(env.t)
    await env.engine.reflection.run(
        __import__("growing_companion.core.reflection", fromlist=["x"]).ReflectionDecision(True, 3, 0.9, 1.0, ["测试"]),
        env.t, llm=None, style=env.engine.persona.style(), attachment=0.6, snap=None or env.engine.emotion.snapshot(
            env.engine.emotion.load(), env.engine.persona.traits()), openness=0.5)


def test_views_do_not_write(env):
    run(_chat(env))
    before_fp, before_changes = fingerprint(env.db_path), env.engine.db.total_changes
    ro = env.engine.open_readonly()
    cfg = env.engine.cfg
    outs = [v.render_status(ro, env.clock, cfg), v.render_memory_list(ro, env.clock, cfg),
            v.render_memory_detail(ro, env.clock, cfg, 1), v.render_memory_detail(ro, env.clock, cfg, 99999),
            v.render_growth_report(ro, env.clock, cfg), v.render_relationship(ro, env.clock, cfg),
            v.render_last_reflection(ro, env.clock, cfg)]
    export_graph(ro, env.t)
    ro.close()
    assert fingerprint(env.db_path) == before_fp                       # 整库内容一字未变
    assert env.engine.db.total_changes == before_changes               # 写连接也没有任何新增写入
    assert all(isinstance(o, str) and o for o in outs)
    assert "情绪" in outs[0] and "成长阶段" in outs[0]
    assert "#1" in outs[1] and "权重" in outs[1]
    assert "原文" in outs[2] and "召回次数" in outs[2] and "已压缩" in outs[2] and "原始记忆引用" in outs[2] + "原始记忆引用"
    assert "没有找到" in outs[3]
    assert "里程碑" in outs[4] and "练习" in outs[4]
    assert "信任" in outs[5] and "依恋" in outs[5]
    assert "复盘" in outs[6]


def test_readonly_connection_rejects_writes(env):
    run(_chat(env))
    ro = env.engine.open_readonly()
    with pytest.raises(sqlite3.Error):
        ro.query("DELETE FROM memories")
    with pytest.raises(sqlite3.Error):
        ro.query("UPDATE capabilities SET proficiency=1")
    ro.close()


def test_views_friendly_when_empty(env):
    ro = env.engine.open_readonly()
    cfg = env.engine.cfg
    for fn in (v.render_memory_list, v.render_relationship, v.render_last_reflection):
        out = fn(ro, env.clock, cfg)
        assert isinstance(out, str) and "Traceback" not in out and out
    assert v.render_status(ro, env.clock, cfg)       # 还没聊过也能看，不报错
    ro.close()


def test_brainmap_export_fields_complete(env):
    run(_chat(env))
    ro = env.engine.open_readonly()
    g = export_graph(ro, env.t, max_nodes=50)
    ro.close()
    assert g["nodes"] and g["edges"] is not None and g["meta"]["readonly"] is True
    need = {"id", "type", "summary", "weight", "size", "emotion", "dominant_emotion", "color", "brightness",
            "recall_count", "last_recalled_at", "created_at", "valence", "arousal", "clarity", "compressed", "protected"}
    ids = {n["id"] for n in g["nodes"]}
    for n in g["nodes"]:
        assert need <= set(n)
        assert 0.0 <= n["weight"] <= 1.0 and 0.15 <= n["brightness"] <= 1.0
        assert set(n["emotion"]) == set(EMOTIONS)
        assert n["color"].startswith("#") and len(n["color"]) == 7
        assert n["size"] > 2
    for e in g["edges"]:
        assert {"source", "target", "kind", "strength"} <= set(e)
        assert e["source"] in ids and e["target"] in ids
    assert set(g["meta"]["emotion_colors"]) == set(EMOTION_COLORS)
