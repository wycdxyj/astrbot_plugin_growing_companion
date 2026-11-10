"""情绪引擎：joy/trust/fear/sadness/curiosity/anger。

支持：触发（appraise）、惯性（高惯性情绪难被快速扭转）、矛盾（相反情绪可共存、
嘴硬心软）、衰减（回到由人格决定的基线）、事后后悔。
所有衰减/投影都是纯函数，查看面板可以“算出现在的她”而不写库。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .db import Database
from .models import EMOTIONS, EmotionSnapshot, Percept, clamp

# 半衰期（分钟）：信任最慢，开心/好奇较快；再乘以 (0.6 + 惯性)
HALF_LIFE_MIN: dict[str, float] = {
    "joy": 90, "trust": 2400, "fear": 120, "sadness": 240, "curiosity": 120, "anger": 150,
}
INERTIA: dict[str, float] = {
    "joy": 0.35, "trust": 0.8, "fear": 0.4, "sadness": 0.6, "curiosity": 0.3, "anger": 0.45,
}


@dataclass
class EmotionState:
    values: dict[str, float]
    baseline: dict[str, float]
    updated_at: float
    suppressed: str = ""        # 被压住的情绪（嘴硬时藏在生气背后的难过）
    regret: float = 0.0         # 事后后悔
    last_peak_anger: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def copy(self) -> "EmotionState":
        return EmotionState(dict(self.values), dict(self.baseline), self.updated_at,
                            self.suppressed, self.regret, self.last_peak_anger, dict(self.extra))


def baseline_from_traits(traits: dict[str, float]) -> dict[str, float]:
    intro = traits.get("introversion", 0.5)
    sens = traits.get("sensitivity", 0.5)
    stub = traits.get("stubbornness", 0.5)
    openness = traits.get("openness", 0.5)
    return {
        "joy": 0.22 + 0.12 * (1 - intro),
        "trust": 0.50,
        "fear": 0.05 + 0.10 * sens,
        "sadness": 0.04 + 0.06 * sens,
        "curiosity": 0.20 + 0.20 * openness,
        "anger": 0.02 + 0.05 * stub,
    }


class EmotionEngine:
    def __init__(self, db: Database, decay_speed: float = 1.0):
        self.db = db
        self.decay_speed = max(0.05, decay_speed)

    # ---------------- 存取 ----------------
    def ensure_seed(self, now: float, traits: dict[str, float]) -> None:
        if self.db.query_one("SELECT 1 FROM emotion_state LIMIT 1"):
            return
        base = baseline_from_traits(traits)
        for e in EMOTIONS:
            self.db.execute(
                "INSERT INTO emotion_state(emotion,value,baseline,inertia,updated_at) VALUES(?,?,?,?,?)",
                (e, base[e], base[e], INERTIA[e], now))
        self.db.execute("INSERT OR IGNORE INTO emotion_meta(id,suppressed,regret,last_peak_anger,updated_at)"
                        " VALUES(1,'',0,0,?)", (now,))

    def load(self, db: Any = None) -> EmotionState:
        src = db or self.db
        rows = src.query("SELECT * FROM emotion_state")
        meta = src.query_one("SELECT * FROM emotion_meta WHERE id=1") or {}
        values = {r["emotion"]: r["value"] for r in rows}
        base = {r["emotion"]: r["baseline"] for r in rows}
        for e in EMOTIONS:
            values.setdefault(e, 0.1)
            base.setdefault(e, 0.1)
        upd = max((r["updated_at"] for r in rows), default=0.0)
        return EmotionState(values, base, upd, meta.get("suppressed") or "",
                            float(meta.get("regret") or 0.0), float(meta.get("last_peak_anger") or 0.0))

    def save(self, st: EmotionState) -> None:
        for e in EMOTIONS:
            self.db.execute("UPDATE emotion_state SET value=?, baseline=?, updated_at=? WHERE emotion=?",
                            (st.values[e], st.baseline[e], st.updated_at, e))
        self.db.execute("UPDATE emotion_meta SET suppressed=?, regret=?, last_peak_anger=?, updated_at=? WHERE id=1",
                        (st.suppressed, st.regret, st.last_peak_anger, st.updated_at))

    # ---------------- 衰减（纯函数） ----------------
    def project(self, st: EmotionState, now: float) -> EmotionState:
        """把情绪向前推到 now：回归基线 + 后悔的产生与消退。不写库。"""
        new = st.copy()
        dt_min = max(0.0, now - st.updated_at) / 60.0
        if dt_min <= 0:
            return new
        for e in EMOTIONS:
            hl = HALF_LIFE_MIN[e] * (0.6 + INERTIA[e]) / self.decay_speed
            base = st.baseline[e]
            new.values[e] = base + (st.values[e] - base) * 0.5 ** (dt_min / hl)
        # 事后后悔：之前气得很凶，现在气消了 → 后悔从“气消的那一刻”起算，再慢慢淡去
        regret_hl = 90.0 / self.decay_speed
        if st.last_peak_anger > 0.45 and new.values["anger"] < 0.25:
            base_a, a0 = st.baseline["anger"], st.values["anger"]
            hl_a = HALF_LIFE_MIN["anger"] * (0.6 + INERTIA["anger"]) / self.decay_speed
            t_cross = hl_a * math.log2((a0 - base_a) / (0.25 - base_a)) if a0 > 0.25 else 0.0
            t_cross = min(max(t_cross, 0.0), dt_min)
            new.regret = max(st.regret, 0.7 * st.last_peak_anger) * 0.5 ** ((dt_min - t_cross) / regret_hl)
            new.last_peak_anger = 0.0
            new.suppressed = ""
        else:
            new.regret = st.regret * 0.5 ** (dt_min / regret_hl)
        if new.regret < 0.03:
            new.regret = 0.0
        new.updated_at = now
        return new

    # ---------------- 评估（规则版情绪触发） ----------------
    def appraise(self, p: Percept, *, longing: float = 0.0, novelty: float = 0.0,
                 attachment: float = 0.5) -> dict[str, float]:
        d: dict[str, float] = {e: 0.0 for e in EMOTIONS}

        def add(**kw: float) -> None:
            for k, v in kw.items():
                d[k] += v

        c = p.cues
        if "praise" in c:
            add(joy=0.25, trust=0.04, sadness=-0.05, curiosity=0.03)
        if "scold" in c:
            add(sadness=0.18, anger=0.14, fear=0.12, joy=-0.10, trust=-0.03)
        if "comfort" in c:
            add(fear=-0.12, sadness=-0.12, trust=0.05, joy=0.06)
        if "apology" in c:
            add(anger=-0.22, sadness=-0.05, trust=0.04, joy=0.03)
        if "joke" in c:
            add(joy=0.12)
        if "greeting" in c:
            add(joy=0.10 + min(0.30, longing * 0.4))
        if "tired" in c:
            add(sadness=0.06, fear=0.03)
        if "farewell" in c:
            add(sadness=0.03 * attachment)
        if "promise" in c:
            add(joy=0.10, curiosity=0.05)
        if "teach" in c:
            add(curiosity=0.10, joy=0.04)
        if "question" in c:
            add(curiosity=0.03)
        if novelty > 0:
            add(curiosity=0.10 * novelty)
        # 久别重逢：不管说什么，见到爸爸就是会开心一点
        if p.gap_s and p.gap_s > 6 * 3600 and longing > 0.2:
            add(joy=0.12 * longing, sadness=-0.05 * longing)
        return d

    def apply(self, st: EmotionState, deltas: dict[str, float], sensitivity: float,
              attachment: float, now: float) -> EmotionState:
        """应用情绪变化。惯性：高值情绪“降”得慢；矛盾：相反情绪只部分抵消。"""
        new = st.copy()
        sens = 0.6 + 0.8 * clamp(sensitivity)
        for e in EMOTIONS:
            d = deltas.get(e, 0.0)
            v = new.values[e]
            if d > 0:
                v += d * sens * (1 - 0.5 * v)
            elif d < 0:
                v += d * (1 - INERTIA[e] * 0.6)
            new.values[e] = clamp(v)
        pos = max(deltas.get("joy", 0), 0)
        if pos > 0:   # 开心也不会让难过立刻消失（矛盾心理）
            new.values["sadness"] = clamp(new.values["sadness"] - 0.25 * pos)
        if deltas.get("anger", 0) > 0 or deltas.get("sadness", 0) > 0:
            new.values["joy"] = clamp(new.values["joy"] - 0.3 * max(deltas.get("anger", 0), deltas.get("sadness", 0)))
        # 嘴硬心软：生气的同时有难过，且很在意对方 → 难过被压在生气后面
        if deltas.get("anger", 0) > 0.05 and deltas.get("sadness", 0) > 0.05 and attachment > 0.45:
            new.suppressed = "sadness"
        new.last_peak_anger = max(new.last_peak_anger, new.values["anger"])
        new.updated_at = now
        return new

    def mood_congruent_delta(self, recalled: list[dict[str, Any]]) -> dict[str, float]:
        """想起强烈情绪的往事，会被带着走一点（关联触发的情绪后效）。"""
        d: dict[str, float] = {e: 0.0 for e in EMOTIONS}
        for m in recalled:
            if m.get("emotion_intensity", 0) < 0.5:
                continue
            vec = m.get("emotion", {})
            for e in EMOTIONS:
                d[e] += 0.08 * vec.get(e, 0.0) * min(1.0, m.get("weight", 0.3) * 1.5)
        return d

    # ---------------- 快照（纯） ----------------
    @staticmethod
    def snapshot(st: EmotionState, traits: dict[str, float], attachment: float = 0.5) -> EmotionSnapshot:
        dev = {e: st.values[e] - st.baseline[e] for e in EMOTIONS}
        order = sorted(EMOTIONS, key=lambda e: dev[e], reverse=True)
        dom = order[0] if dev[order[0]] > 0.08 else "trust"
        sec = order[1] if dev[order[1]] > 0.12 and order[1] != dom else None
        intensity = clamp(max(0.0, dev[order[0]]))
        v = st.values
        valence = clamp(max(dev["joy"], 0) + 0.5 * max(dev["trust"], 0) + 0.15 * v["curiosity"]
                        - v["sadness"] - 0.8 * v["anger"] - 0.7 * v["fear"], -1, 1)
        arousal = clamp(max(v["anger"], v["fear"], max(dev["joy"], 0), v["curiosity"] * 0.7))
        strong = [e for e in EMOTIONS if dev[e] > 0.2]
        mixed = len(strong) >= 2
        tsundere = (v["anger"] > 0.3 and attachment > 0.45 and traits.get("stubbornness", 0.5) > 0.35
                    and (st.suppressed != "" or v["sadness"] > 0.2))
        return EmotionSnapshot(values=dict(v), dominant=dom, secondary=sec, intensity=intensity,
                               valence=valence, arousal=arousal, mixed=mixed, tsundere=tsundere,
                               regret=st.regret)
