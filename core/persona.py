"""人格与状态层：人格底色、语言风格与口癖、身体感、注意力与不在场感。

- 人格底色是连续数值，随成长缓慢漂移（有上限），不会每轮重置
- 身体感不是真生理，是“状态驱动表达”：精力/困倦/刚睡醒会影响表达长度和语气
- 注意力允许“不在场”：她可能在忙别的、没注意到
"""
from __future__ import annotations

import math
import random
import re
from typing import Any

from .clock import Clock
from .db import Database
from .models import AttentionState, BodyState, clamp, jdump, jload

TRAIT_KEYS = ("introversion", "sensitivity", "patience", "clinginess", "humor", "stubbornness", "openness")
MAX_TRAIT_DRIFT = 0.2   # 人格漂移幅度上限：会变，但不会变成另一个人

DEFAULT_STYLE: dict[str, Any] = {
    "name": "小满",
    "address_father": "爸爸",
    "catchphrases": ["嗯嗯", "诶~"],
    "particles": ["呀", "啦", "嘛"],
    "happy_style": "尾音上扬、爱用叠词和“嘿嘿”，话会变多一点",
    "angry_style": "句子变短、先“哼”一声、有时故意不叫爸爸，但很容易心软",
    "sleepy_style": "拖长音、断句、偶尔打哈欠，可能口齿不清",
    "learned_phrases": {},   # 从父亲那里模仿来的口癖：{短语: 出现次数}
}

# 模仿学习：父亲反复使用的口头禅会被她慢慢学去（不是复制，是渐进内化）
_PHRASE_RE = re.compile(r"(哈哈+|嘿嘿|好吧|对吧|是吧|嗯哼|哎呀|真的假的|绝了|离谱|行吧|乖乖|没毛病|妥了)")


class PersonaStore:
    def __init__(self, db: Database):
        self.db = db

    def ensure_seed(self, now: float, init: dict[str, Any]) -> None:
        if self.db.query_one("SELECT 1 FROM persona_traits LIMIT 1"):
            return
        for k in TRAIT_KEYS:
            v = clamp(float(init.get(k, 0.5)))
            self.db.execute("INSERT INTO persona_traits(key,value,initial,updated_at) VALUES(?,?,?,?)",
                            (k, v, v, now))
        style = dict(DEFAULT_STYLE)
        if init.get("name"):
            style["name"] = str(init["name"])
        for k, v in style.items():
            self.db.execute("INSERT INTO persona_style(key,value_json,updated_at) VALUES(?,?,?)",
                            (k, jdump(v), now))

    @staticmethod
    def read_traits(src: Any) -> dict[str, float]:
        t = {r["key"]: float(r["value"]) for r in src.query("SELECT key,value FROM persona_traits")}
        for k in TRAIT_KEYS:
            t.setdefault(k, 0.5)
        return t

    @staticmethod
    def read_style(src: Any) -> dict[str, Any]:
        s = dict(DEFAULT_STYLE)
        for r in src.query("SELECT key,value_json FROM persona_style"):
            s[r["key"]] = jload(r["value_json"], s.get(r["key"]))
        return s

    def traits(self) -> dict[str, float]:
        return self.read_traits(self.db)

    def style(self) -> dict[str, Any]:
        return self.read_style(self.db)

    def set_style(self, key: str, value: Any, now: float) -> None:
        self.db.execute("INSERT INTO persona_style(key,value_json,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                        (key, jdump(value), now))

    def nudge_trait(self, key: str, delta: float, now: float) -> None:
        """人格缓慢漂移，相对出厂值最多 ±MAX_TRAIT_DRIFT。"""
        row = self.db.query_one("SELECT value, initial FROM persona_traits WHERE key=?", (key,))
        if not row:
            return
        v = clamp(row["value"] + delta, row["initial"] - MAX_TRAIT_DRIFT, row["initial"] + MAX_TRAIT_DRIFT)
        self.db.execute("UPDATE persona_traits SET value=?, updated_at=? WHERE key=?", (clamp(v), now, key))

    def learn_phrases(self, text: str, now: float) -> list[str]:
        """观察学习：父亲口头禅出现 ≥3 次后，她才可能开始用（由规划器按概率决定）。"""
        found = _PHRASE_RE.findall(text or "")
        if not found:
            return []
        style = self.style()
        learned: dict[str, int] = dict(style.get("learned_phrases", {}))
        newly: list[str] = []
        for ph in found:
            learned[ph] = learned.get(ph, 0) + 1
            if learned[ph] == 3:
                newly.append(ph)
        self.set_style("learned_phrases", learned, now)
        return newly

    @staticmethod
    def trait_words(t: dict[str, float]) -> str:
        """把连续数值翻译成稳定的性格描述（分档，便于固定层缓存）。"""
        def lv(x: float, lo: str, mid: str, hi: str) -> str:
            return lo if x < 0.38 else hi if x > 0.62 else mid
        parts = [
            lv(t["introversion"], "偏外向、爱说话", "不算内向也不算外向", "偏内向、慢热"),
            lv(t["sensitivity"], "心大、不太敏感", "有点敏感", "很敏感、容易察觉别人的情绪"),
            lv(t["patience"], "没什么耐心、容易急", "耐心一般", "有耐心"),
            lv(t["clinginess"], "比较独立", "有点黏人", "很黏人、怕被冷落"),
            lv(t["humor"], "不太会开玩笑", "偶尔开玩笑", "爱开玩笑、爱调侃"),
            lv(t["stubbornness"], "好说话", "有点倔", "很倔、认定的事不爱改口"),
        ]
        return "；".join(parts)


# --------------------------------------------------------------------------- #
# 身体感
# --------------------------------------------------------------------------- #

# 各阶段的作息窗口（小时）：(入睡, 起床)。不是闹钟，只决定“精力曲线”。
STAGE_SLEEP = {"infant": (19.5, 7.0), "child": (21.0, 7.0), "teen": (23.0, 7.5), "young_adult": (24.0, 7.5)}


def circadian(hour: float, stage: str) -> tuple[float, float, bool, float]:
    """返回 (精力目标, 困倦目标, 是否在睡眠窗口内, 起床后已过秒数)。"""
    sleep_h, wake_h = STAGE_SLEEP.get(stage, (22.0, 7.0))
    sleep_h %= 24
    asleep = (hour >= sleep_h or hour < wake_h) if sleep_h > wake_h else (sleep_h <= hour < wake_h)
    since_wake_h = (hour - wake_h) % 24
    if asleep:
        return 0.12, 0.95, True, 0.0
    active_len = (sleep_h - wake_h) % 24 or 16.0
    frac = clamp(since_wake_h / active_len)
    energy = 0.35 + 0.55 * math.sin(math.pi * frac)
    sleepiness = clamp(0.9 - energy + (0.2 if frac > 0.85 else 0.0))
    return energy, sleepiness, False, since_wake_h * 3600


class BodyModel:
    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock

    def ensure_seed(self, now: float) -> None:
        self.db.execute("INSERT OR IGNORE INTO body_state(id,energy,sleepiness,phase,groggy_until,"
                        "woken_by_user,updated_at) VALUES(1,0.7,0.2,'awake',0,0,?)", (now,))

    @staticmethod
    def read(src: Any) -> BodyState:
        r = src.query_one("SELECT * FROM body_state WHERE id=1")
        if not r:
            return BodyState()
        return BodyState(r["energy"], r["sleepiness"], r["phase"], r["groggy_until"] or 0.0,
                         bool(r["woken_by_user"]), r["updated_at"] or 0.0)

    def load(self) -> BodyState:
        return self.read(self.db)

    def save(self, b: BodyState) -> None:
        self.db.execute("UPDATE body_state SET energy=?, sleepiness=?, phase=?, groggy_until=?, "
                        "woken_by_user=?, updated_at=? WHERE id=1",
                        (b.energy, b.sleepiness, b.phase, b.groggy_until, int(b.woken_by_user), b.updated_at))

    def step(self, b: BodyState, now: float, stage: str) -> BodyState:
        """纯函数：把身体状态推进到 now（精力向昼夜节律目标松弛）。"""
        hour = self.clock.hour_float(now)
        e_t, s_t, asleep, since_wake = circadian(hour, stage)
        dt_min = max(0.0, now - (b.updated_at or now)) / 60.0
        k = 1 - 0.5 ** (dt_min / 60.0)    # 约 1 小时时间常数
        new = BodyState(
            energy=clamp(b.energy + (e_t - b.energy) * k),
            sleepiness=clamp(b.sleepiness + (s_t - b.sleepiness) * k),
            phase=b.phase, groggy_until=b.groggy_until, woken_by_user=b.woken_by_user, updated_at=now)
        # 刚睡醒：上一刻还在睡，现在起床不久 → 迷糊一阵
        if b.phase == "asleep" and not asleep and since_wake < 2400:
            new.groggy_until = max(new.groggy_until, now + (2400 - since_wake))
            new.woken_by_user = False
        if new.groggy_until > now:
            new.phase = "groggy"
        elif asleep:
            new.phase, new.woken_by_user = "asleep", False
        elif new.sleepiness > 0.65:
            new.phase = "drowsy"
        else:
            new.phase = "awake"
        return new

    def wake_by_message(self, b: BodyState, now: float) -> BodyState:
        """睡眠窗口内收到消息：被吵醒，迷糊一阵（不是永远秒回、永远清醒）。"""
        if b.phase == "asleep":
            b.phase, b.woken_by_user, b.groggy_until = "groggy", True, now + 900
        return b

    @staticmethod
    def spend(b: BodyState, arousal: float, text_len: int) -> BodyState:
        """聊天会消耗一点精力。"""
        b.energy = clamp(b.energy - 0.006 - 0.012 * arousal - min(0.01, text_len / 8000))
        return b

    @staticmethod
    def expression_factor(b: BodyState) -> float:
        """精力/困倦对表达长度的影响系数。"""
        f = 0.55 + 0.6 * b.energy - 0.25 * b.sleepiness
        if b.phase == "groggy":
            f *= 0.7
        return clamp(f, 0.35, 1.15)


# --------------------------------------------------------------------------- #
# 注意力与社交需求
# --------------------------------------------------------------------------- #

AWAY_REASONS = ["在摆弄自己的小东西", "在发呆", "在看窗外", "在想事情", "在玩", "在哼歌"]


class AttentionModel:
    def __init__(self, db: Database):
        self.db = db

    def ensure_seed(self, now: float) -> None:
        self.db.execute("INSERT OR IGNORE INTO attention_state(id,focus,away_until,away_reason,social_need,"
                        "solitude_need,updated_at) VALUES(1,0.8,0,'',0.4,0.2,?)", (now,))

    @staticmethod
    def read(src: Any) -> AttentionState:
        r = src.query_one("SELECT * FROM attention_state WHERE id=1")
        if not r:
            return AttentionState()
        return AttentionState(r["focus"], r["away_until"] or 0.0, r["away_reason"] or "",
                              r["social_need"], r["solitude_need"], r["updated_at"] or 0.0)

    def load(self) -> AttentionState:
        return self.read(self.db)

    def save(self, a: AttentionState) -> None:
        self.db.execute("UPDATE attention_state SET focus=?, away_until=?, away_reason=?, social_need=?, "
                        "solitude_need=?, updated_at=? WHERE id=1",
                        (a.focus, a.away_until, a.away_reason, a.social_need, a.solitude_need, a.updated_at))

    def drift(self, a: AttentionState, now: float, traits: dict[str, float], body: BodyState,
              longing: float, rng: random.Random) -> AttentionState:
        """随时间自然漂移：可能走神/去忙别的；想念会积累社交需求；聊多了会想独处。"""
        dt_h = max(0.0, now - (a.updated_at or now)) / 3600.0
        new = AttentionState(a.focus, a.away_until, a.away_reason, a.social_need, a.solitude_need, now)
        if new.away_until and new.away_until <= now:
            new.away_until, new.away_reason = 0.0, ""
        if not new.away_until and body.phase in ("awake", "drowsy"):
            distract = 0.15 * (1.1 - traits.get("patience", 0.5) * 0.5) * (0.4 + body.energy)
            if rng.random() < 1 - math.exp(-distract * min(dt_h, 6.0)):
                new.away_until = now + rng.uniform(300, 2400)
                new.away_reason = rng.choice(AWAY_REASONS)
        cl = traits.get("clinginess", 0.5)
        new.social_need = clamp(a.social_need + (0.06 * cl + 0.08 * longing) * min(dt_h, 12.0))
        relax = 1 - 0.5 ** (dt_h / 6.0)
        new.solitude_need = clamp(a.solitude_need + (0.15 - a.solitude_need) * relax)
        new.focus = clamp(a.focus + ((0.2 if new.away_until else 0.8) - a.focus) * relax)
        return new

    @staticmethod
    def on_message(a: AttentionState, now: float, traits: dict[str, float]) -> tuple[AttentionState, bool]:
        """收到消息：返回 (新状态, 刚才是否不在场)。被关注会满足一部分社交需求；聊多了更想独处。"""
        was_away = bool(a.away_until and a.away_until > now)
        a.away_until, a.away_reason = 0.0, ""
        a.focus = clamp(a.focus + 0.3)
        a.social_need = clamp(a.social_need - 0.22)
        a.solitude_need = clamp(a.solitude_need + 0.03 + 0.04 * traits.get("introversion", 0.5))
        a.updated_at = now
        return a, was_away
