"""生活流 tick：时间流逝 → 状态更新。

tick 不强制任何固定行为，只更新内部状态：情绪衰减、身体节律、注意力漂移、想念积累、
记忆权重刷新（衰减/沉睡）、能力生疏与自己练习、阶段检查、睡醒后可能的梦。
引擎在每次收到消息时会“补一次 tick”（追上流逝的时间），后台任务则以带抖动的间隔调用。
"""
from __future__ import annotations

from typing import Any

from .models import clamp
from .relationship import RelationshipSystem


class LifeTick:
    @staticmethod
    def run(eng: Any, now: float) -> dict[str, Any]:
        ts = eng.db.query_one("SELECT * FROM time_sense WHERE id=1") or {}
        last = ts.get("last_tick_at") or now
        dt = max(0.0, now - last)
        if dt < 1.0:
            return {}
        dt_h = dt / 3600.0
        out: dict[str, Any] = {"dt_s": dt}
        traits = eng.persona.traits()
        stage = eng.growth.state()["stage"]

        # 1) 情绪：回归基线（含后悔的产生/消退）+ 久别的失落
        est0 = eng.emotion.load()
        est = eng.emotion.project(est0, now)
        rel = eng.relations.father()
        if rel:
            RelationshipSystem.update_longing(rel, now, traits.get("clinginess", 0.5))
            if rel.longing > 0.3:
                est.values["sadness"] = clamp(est.values["sadness"] + 0.03 * rel.longing * min(dt_h, 6.0))
            eng.relations.save(rel)
        eng.emotion.save(est)
        # 难过/害怕后自己平复下来 → 练习“自我安抚”（情绪调节：由状态驱动，不是定时）
        calmed = (max(est0.values["sadness"], est0.values["fear"]) > 0.35
                  and max(est.values["sadness"], est.values["fear"]) < 0.25)

        # 2) 身体感：昼夜节律；刚睡醒可能带着一个梦
        body0 = eng.body_model.load()
        body = eng.body_model.step(body0, now, stage)
        eng.body_model.save(body)
        if body0.phase == "asleep" and body.phase != "asleep":
            did = eng.inner.maybe_dream(now, eng.rng)
            if did:
                out["dream"] = did

        # 3) 注意力与社交需求
        att = eng.attention_model.drift(eng.attention_model.load(), now, traits, body,
                                        rel.longing if rel else 0.0, eng.rng)
        eng.attention_model.save(att)

        # 4) 记忆：衰减刷新（权重/清晰度/沉睡）
        out["memories_refreshed"] = eng.memory.refresh_weights(now)

        # 5) 成长：生疏/退步、自己练习、阶段检查
        events = eng.growth.decay(now, dt)
        events += eng.growth.self_practice(now, body, min(dt_h, 6.0), eng.rng)
        if calmed:
            events += eng.growth.practice("self_soothe", 0.6, body, now)
        st_ev = eng.growth.check_stage(now)
        if st_ev:
            events.append(st_ev)
        eng.handle_growth_events(events, now)
        out["growth_events"] = [e.kind + ":" + e.key for e in events]

        eng.db.execute("UPDATE time_sense SET last_tick_at=? WHERE id=1", (now,))
        return out

    @staticmethod
    def next_interval(base_s: float, rng: Any) -> float:
        """后台 tick 的间隔带抖动（±50%），不是整点闹钟。"""
        return max(30.0, base_s * rng.uniform(0.5, 1.5))
