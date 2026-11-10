"""成长引擎：阶段 + 能力池 + 熟练度 + 渐进解锁。

成长不是“满 X 天自动解锁 Y 技能”，而是 时间 + 学习进度 + 经历 + 练习次数 + 认知/身体状态 共同驱动：
- 能力有熟练度 0~1，练习才涨，边际递减；经历不足/认知不足/前置能力不足/状态差时涨得更慢
- 解锁 = 阶段到了 + 经历够 + 练习够 + 认知够 + 前置够 + 熟练度过阈值（缺一不可）
- 允许退步：很久不练会生疏；状态差时“表现”下降；跌破阈值会从“已会”退回“还在学”
- 第一次学会某件事 → 返回 unlock 事件，由引擎写成里程碑和重要情景记忆
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

from .clock import Clock
from .db import Database
from .models import (EmotionSnapshot, BodyState, GrowthProfile, Percept, STAGES, clamp, jdump, stage_index)
from .cognition import style_for


@dataclass(frozen=True)
class CapDef:
    key: str
    domain: str            # language / social / life / emotion / cognition
    title: str
    stage_min: str
    req_experience: int    # 需要见识/经历过的次数
    req_practice: int      # 需要练习的次数
    req_cognition: float   # 需要的认知水平
    min_energy: float      # 精力低于此值时学得差
    unlock_threshold: float
    prereq: dict[str, float] = field(default_factory=dict)
    cue_in: str = ""       # 父亲的话里出现 → 增加“经历”
    cue_out: str = ""      # 她的回复里出现 → 算一次“练习”


CAP_POOL: list[CapDef] = [
    # —— 语言：咿呀 → 蹦词 → 短句 → 完整表达 → 复杂表达（逐级，不是某天突然会说话）
    CapDef("babble", "language", "咿呀学语", "infant", 0, 4, 0.0, 0.15, 0.30),
    CapDef("first_words", "language", "蹦词", "infant", 8, 10, 0.05, 0.20, 0.35, {"babble": 0.3}),
    CapDef("short_sentences", "language", "说短句", "child", 40, 40, 0.15, 0.25, 0.40, {"first_words": 0.5}),
    CapDef("full_sentences", "language", "完整表达", "child", 120, 100, 0.30, 0.25, 0.45, {"short_sentences": 0.5}),
    CapDef("complex_expression", "language", "复杂表达", "teen", 300, 200, 0.50, 0.30, 0.50, {"full_sentences": 0.6}),
    # —— 社交
    CapDef("call_dad", "social", "叫爸爸", "infant", 5, 6, 0.02, 0.20, 0.35, {"babble": 0.3}, r"叫爸爸|叫我爸爸|喊爸爸|我是爸爸|爸爸在这", r"爸"),
    CapDef("greet", "social", "打招呼", "child", 10, 8, 0.10, 0.20, 0.40, {"first_words": 0.4}, r"早安|晚安|你好|再见|拜拜", r"早|晚安|你好|嗨|再见|拜拜"),
    CapDef("comfort_others", "social", "安慰别人", "child", 20, 12, 0.25, 0.25, 0.40, {"short_sentences": 0.3}, r"累|难过|不开心|心情不好|难受", r"抱抱|没事|别难过|别累|摸摸|休息"),
    # —— 生活技能：试错与练习累积
    CapDef("hold_chopsticks", "life", "拿筷子", "child", 15, 25, 0.10, 0.35, 0.50, {}, r"筷子|吃饭|夹菜", r"筷子|夹"),
    CapDef("dressing", "life", "自己穿衣服", "child", 15, 25, 0.10, 0.35, 0.50, {}, r"穿衣|穿鞋|扣扣子|拉链|衣服", r"穿|扣|拉链"),
    CapDef("tidying", "life", "整理东西", "child", 15, 20, 0.12, 0.35, 0.50, {}, r"收拾|整理|玩具|放好", r"收拾|整理|放好|放回"),
    # —— 情绪
    CapDef("name_feelings", "emotion", "说出自己的感受", "child", 25, 15, 0.20, 0.25, 0.40, {"short_sentences": 0.3}, r"开心|难过|生气|害怕|高兴|伤心", r"开心|难过|生气|害怕|高兴|伤心"),
    CapDef("self_soothe", "emotion", "自我安抚", "child", 10, 10, 0.25, 0.20, 0.45),
    # —— 认知
    CapDef("ask_why", "cognition", "追问为什么", "child", 15, 12, 0.10, 0.25, 0.40, {"short_sentences": 0.3}, r"因为|是这样的|原因", r"为什么|为啥"),
    CapDef("causal_reasoning", "cognition", "因果推理", "teen", 60, 40, 0.35, 0.30, 0.45, {"full_sentences": 0.5}, r"因为|所以|如果|那就", r"因为|所以"),
    CapDef("hypothetical", "cognition", "假设思考", "teen", 100, 60, 0.50, 0.30, 0.45, {"causal_reasoning": 0.4}, r"如果|假如|要是", r"如果|要是|假如"),
    CapDef("independent_opinion", "cognition", "有自己的看法", "teen", 50, 30, 0.40, 0.30, 0.45, {"full_sentences": 0.5}, r"你觉得|你怎么看|你认为", r"我觉得|我不想|才不|我不同意|我认为"),
    CapDef("counterfactual", "cognition", "反事实思考", "young_adult", 150, 80, 0.60, 0.30, 0.50, {"hypothetical": 0.5}, r"早知道|当初|要是.*就", r"早知道|当初|要是.*就"),
]
CAP_BY_KEY = {c.key: c for c in CAP_POOL}

# 语言能力 → 表达档位
LANG_TIER_KEYS = [("first_words", 1), ("short_sentences", 2), ("full_sentences", 3), ("complex_expression", 4)]
LANG_LABEL = {
    0: "只会咿呀声和单音节叠词（如“啊”“爸”“嗯嗯”），不会说成句的话",
    1: "只会蹦单词和两三个字的词，语法不完整",
    2: "能说简单的短句，每句话很短，常常词不达意",
    3: "能比较完整地说话，句子简单直接，用词是孩子/少年的水平",
    4: "能做复杂表达：解释原因、打比方、说出矛盾复杂的心情",
}
LANG_MAX_CHARS = {0: 4, 1: 6, 2: 30, 3: 90, 4: 220}

# 阶段晋升：时间(虚拟天) + 技能 + 认知 + 经历 + 里程碑 共同满足
STAGE_RULES: dict[str, dict[str, float]] = {
    "infant": {"min_days": 5, "avg_prof": 0.50, "unlocked_ratio": 0.66, "cognition": 0.12, "experience": 30, "milestones": 2},
    "child": {"min_days": 60, "avg_prof": 0.45, "unlocked_ratio": 0.60, "cognition": 0.35, "experience": 300, "milestones": 6},
    "teen": {"min_days": 180, "avg_prof": 0.50, "unlocked_ratio": 0.60, "cognition": 0.60, "experience": 1500, "milestones": 12},
}


@dataclass
class GrowthEvent:
    kind: str     # unlock / relearn / regress / stage
    key: str
    title: str
    stage: str = ""


class GrowthEngine:
    def __init__(self, db: Database, clock: Clock, speed: float = 1.0, rng: random.Random | None = None):
        self.db = db
        self.clock = clock
        self.speed = max(0.05, speed)
        self.rng = rng or random.Random()

    # ---------------- 初始化 ----------------
    def ensure_seed(self, now: float, initial_stage: str = "infant") -> None:
        if not self.db.query_one("SELECT 1 FROM growth_state"):
            self.db.execute("INSERT INTO growth_state(id,stage,stage_since,cognition_level,total_experience,updated_at)"
                            " VALUES(1,?,?,?,0,?)", (initial_stage, now, 0.05, now))
        for d in CAP_POOL:
            self.db.execute("INSERT OR REPLACE INTO capability_defs(key,domain,title,stage_min,req_experience,"
                            "req_practice,req_cognition,min_energy,unlock_threshold,prereq_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (d.key, d.domain, d.title, d.stage_min, d.req_experience, d.req_practice,
                             d.req_cognition, d.min_energy, d.unlock_threshold, jdump(d.prereq)))
            self.db.execute("INSERT OR IGNORE INTO capabilities(key,updated_at) VALUES(?,?)", (d.key, now))

    # ---------------- 读取 ----------------
    @staticmethod
    def read_state(src: Any) -> dict[str, Any]:
        return src.query_one("SELECT * FROM growth_state WHERE id=1") or {
            "stage": "infant", "stage_since": 0.0, "cognition_level": 0.05, "total_experience": 0}

    def state(self) -> dict[str, Any]:
        return self.read_state(self.db)

    @staticmethod
    def read_caps(src: Any) -> dict[str, dict[str, Any]]:
        return {r["key"]: r for r in src.query("SELECT * FROM capabilities")}

    def caps(self) -> dict[str, dict[str, Any]]:
        return self.read_caps(self.db)

    # ---------------- 解锁判定（纯函数） ----------------
    @staticmethod
    def eligibility(d: CapDef, c: dict[str, Any], stage: str, cognition: float, energy: float,
                    caps: dict[str, dict[str, Any]]) -> dict[str, Any]:
        stage_ok = stage_index(stage) >= stage_index(d.stage_min)
        prereq_ratio = min((min(1.0, caps.get(k, {}).get("proficiency", 0.0) / v) for k, v in d.prereq.items()),
                           default=1.0)
        exp_ratio = min(1.0, c["experience_count"] / d.req_experience) if d.req_experience else 1.0
        cog_ratio = min(1.0, cognition / d.req_cognition) if d.req_cognition > 0 else 1.0
        energy_ok = energy >= d.min_energy
        factor = ((1.0 if stage_ok else 0.0) * prereq_ratio * (0.3 + 0.7 * exp_ratio)
                  * (0.4 + 0.6 * cog_ratio) * (1.0 if energy_ok else 0.4))
        hard_ok = (stage_ok and prereq_ratio >= 1.0 and exp_ratio >= 1.0 and cog_ratio >= 1.0
                   and c["practice_count"] >= d.req_practice)
        return {"stage_ok": stage_ok, "prereq_ratio": prereq_ratio, "exp_ratio": exp_ratio,
                "cog_ratio": cog_ratio, "energy_ok": energy_ok, "factor": factor, "hard_ok": hard_ok}

    @staticmethod
    def state_for(d: CapDef, prof: float, elig: dict[str, Any]) -> str:
        if not elig["stage_ok"]:
            return "locked"
        if prof >= 0.85 and elig["hard_ok"]:
            return "mastered"
        if prof >= d.unlock_threshold and elig["hard_ok"]:
            return "unlocked"
        return "emerging" if prof > 0.0 else "locked"

    @staticmethod
    def performance(prof: float, body: BodyState, snap: EmotionSnapshot | None) -> float:
        """表现 ≠ 熟练度：精力差、情绪差、刚睡醒时，会的事也会做不好（允许反复）。"""
        f = 0.6 + 0.4 * body.energy
        if body.phase == "groggy":
            f *= 0.85
        if snap:
            v = snap.values
            f *= 1 - 0.3 * max(v.get("anger", 0) * 0.5, v.get("fear", 0) * 0.5, v.get("sadness", 0) * 0.4)
        return clamp(prof * f)

    # ---------------- 经历与练习 ----------------
    def observe(self, p: Percept, now: float) -> None:
        """父亲说的话是“经历/见识”：同一个领域见得多，学起来才快。语言类每次交流都算一次耳濡目染。"""
        st = self.state()
        self.db.execute("UPDATE growth_state SET total_experience=total_experience+1, updated_at=? WHERE id=1", (now,))
        for d in CAP_POOL:
            hit = d.domain == "language" or (d.cue_in and re.search(d.cue_in, p.text))
            if d.key == "call_dad" and "call_dad" in p.cues:
                hit = True
            if hit:
                self.db.execute("UPDATE capabilities SET experience_count=experience_count+1, updated_at=? WHERE key=?",
                                (now, d.key))

    def practice(self, key: str, quality: float, body: BodyState, now: float) -> list[GrowthEvent]:
        d = CAP_BY_KEY[key]
        st = self.state()
        caps = self.caps()
        c = caps[key]
        el = self.eligibility(d, c, st["stage"], st["cognition_level"], body.energy, caps)
        if not el["stage_ok"] or el["factor"] < 0.03:
            return []          # 阶段没到/前置完全不具备：只能“见识”，练了也算不上练习
        old_prof, old_state = c["proficiency"], c["unlock_state"]
        gain = 0.06 * self.speed * clamp(quality) * el["factor"] * (1 - old_prof) ** 0.7
        prof = clamp(old_prof + gain)
        c2 = dict(c, proficiency=prof, practice_count=c["practice_count"] + 1)
        new_state = self.state_for(d, prof, self.eligibility(d, c2, st["stage"], st["cognition_level"], body.energy, caps))
        first = c["first_unlocked_at"]
        events: list[GrowthEvent] = []
        if new_state in ("unlocked", "mastered") and old_state not in ("unlocked", "mastered"):
            events.append(GrowthEvent("unlock" if first is None else "relearn", key, d.title, st["stage"]))
            first = first or now
        self.db.execute("UPDATE capabilities SET proficiency=?, peak=MAX(peak,?), practice_count=practice_count+1, "
                        "unlock_state=?, first_unlocked_at=?, last_practiced_at=?, updated_at=? WHERE key=?",
                        (prof, prof, new_state, first, now, now, key))
        return events

    def after_reply(self, reply: str, p: Percept, body: BodyState, snap: EmotionSnapshot, now: float) -> list[GrowthEvent]:
        """根据她这次说了什么，判定练习了哪些能力。语言能力练“当前最前沿”的那一级。"""
        events: list[GrowthEvent] = []
        caps = self.caps()
        st = self.state()
        q = clamp(0.5 + 0.3 * min(1.0, len(p.text) / 30.0) + 0.2 * (snap.values.get("curiosity", 0)))
        # 语言：每次交流，所有“阶段够得着、还没精通”的语言能力一起练；
        # 前置/经历/认知不足的会因为 factor 低而几乎不涨——不是想学就能学会。
        for d in CAP_POOL:
            if d.domain != "language":
                continue
            if stage_index(st["stage"]) >= stage_index(d.stage_min) and caps[d.key]["unlock_state"] != "mastered":
                events += self.practice(d.key, q, body, now)
        for d in CAP_POOL:
            if d.domain == "language" or not d.cue_out:
                continue
            if re.search(d.cue_out, reply):
                events += self.practice(d.key, q, body, now)
        return events

    def self_practice(self, now: float, body: BodyState, hours: float, rng: random.Random) -> list[GrowthEvent]:
        """生活流：她自己玩、自己试，也会练（低概率、只在精力还行时；不是定时任务）。"""
        events: list[GrowthEvent] = []
        if body.phase in ("asleep", "groggy") or body.energy < 0.35:
            return events
        st = self.state()
        caps = self.caps()
        for d in CAP_POOL:
            if d.domain not in ("life", "emotion") and d.key != "babble":
                continue
            if stage_index(st["stage"]) < stage_index(d.stage_min):
                continue
            p = min(0.5, 0.04 * hours * body.energy)
            if rng.random() < p:
                events += self.practice(d.key, 0.45, body, now)
        return events

    # ---------------- 退步与反复 ----------------
    def decay(self, now: float, dt: float) -> list[GrowthEvent]:
        """很久不练会生疏（有下限）；跌破阈值会从“已会”退回“还在学”。"""
        events: list[GrowthEvent] = []
        dt_days = max(0.0, dt) / 86400.0
        if dt_days <= 0:
            return events
        for key, c in self.caps().items():
            prof = c["proficiency"]
            if prof <= 0 or not c["last_practiced_at"]:
                continue
            idle = (now - c["last_practiced_at"]) / 86400.0
            if idle <= 2 + 6 * prof:
                continue
            loss = 0.01 * (1 - 0.5 * prof) * dt_days
            floor = 0.4 * c["peak"]
            new_prof = max(floor, prof - loss)
            if new_prof >= prof:
                continue
            d = CAP_BY_KEY[key]
            state = c["unlock_state"]
            if state in ("unlocked", "mastered") and new_prof < d.unlock_threshold - 0.08:
                state = "emerging"
                events.append(GrowthEvent("regress", key, d.title))
                self.db.execute("UPDATE capabilities SET regress_count=regress_count+1 WHERE key=?", (key,))
            self.db.execute("UPDATE capabilities SET proficiency=?, unlock_state=?, updated_at=? WHERE key=?",
                            (new_prof, state, now, key))
        return events

    def bump_cognition(self, amount: float, now: float) -> None:
        self.db.execute("UPDATE growth_state SET cognition_level=MIN(1.0, cognition_level+?), updated_at=? WHERE id=1",
                        (amount * self.speed, now))

    # ---------------- 阶段 ----------------
    def check_stage(self, now: float) -> GrowthEvent | None:
        st = self.state()
        cur = st["stage"]
        rule = STAGE_RULES.get(cur)
        if not rule:
            return None
        v_days = (now - st["stage_since"]) / 86400.0 * self.speed
        pool = [d for d in CAP_POOL if d.stage_min == cur]
        caps = self.caps()
        profs = [caps[d.key]["proficiency"] for d in pool]
        unlocked = [d for d in pool if caps[d.key]["unlock_state"] in ("unlocked", "mastered")]
        n_ms = (self.db.query_one("SELECT COUNT(*) n FROM milestones") or {"n": 0})["n"]
        ok = (v_days >= rule["min_days"]
              and (sum(profs) / len(profs) if profs else 1.0) >= rule["avg_prof"]
              and (len(unlocked) / len(pool) if pool else 1.0) >= rule["unlocked_ratio"]
              and st["cognition_level"] >= rule["cognition"]
              and st["total_experience"] >= rule["experience"]
              and n_ms >= rule["milestones"])
        if not ok:
            return None
        nxt = STAGES[stage_index(cur) + 1].value
        self.db.execute("UPDATE growth_state SET stage=?, stage_since=?, updated_at=? WHERE id=1", (nxt, now, now))
        return GrowthEvent("stage", nxt, f"进入{nxt}阶段", nxt)

    # ---------------- 画像（给规划器与提示词） ----------------
    @classmethod
    def build_profile(cls, st: dict[str, Any], caps: dict[str, dict[str, Any]], body: BodyState,
                      snap: EmotionSnapshot | None) -> GrowthProfile:
        perf = {k: cls.performance(c["proficiency"], body, snap) for k, c in caps.items()}
        tier = 0
        for key, t in LANG_TIER_KEYS:
            if caps.get(key, {}).get("unlock_state") in ("unlocked", "mastered"):
                tier = max(tier, t)
        # 状态很差时表达能力暂时下降一档（情绪差/没精神时“说不好”）
        top_key = next((k for k, t in LANG_TIER_KEYS if t == tier), None)
        if top_key and tier > 0:
            prof = caps[top_key]["proficiency"]
            if prof > 0 and perf[top_key] < 0.6 * prof:
                tier -= 1
        # “还在学”：下一级能力已萌芽但没解锁 → 偶尔尝试说更高级的，说不好
        nxt_key = next((k for k, t in LANG_TIER_KEYS if t == tier + 1), None)
        stretch = 0.0
        if nxt_key and caps.get(nxt_key, {}).get("unlock_state") == "emerging":
            thr = CAP_BY_KEY[nxt_key].unlock_threshold
            stretch = clamp(0.6 * caps[nxt_key]["proficiency"] / thr)
        level = st["cognition_level"]
        return GrowthProfile(stage=st["stage"], language_tier=tier, language_label=LANG_LABEL[tier],
                             max_chars=LANG_MAX_CHARS[tier], stretch_prob=stretch, cognition_level=level,
                             cognitive_style=style_for(st["stage"], level), performance=perf)

    def profile(self, body: BodyState, snap: EmotionSnapshot | None) -> GrowthProfile:
        return self.build_profile(self.state(), self.caps(), body, snap)
