"""认知引擎：形成信念/因果/价值判断/世界模型；元认知（我不懂）、心智理论（猜父亲的想法，会误会）。

MVP 为规则版：先把数据结构和“会不同意、会说不懂、会误会再修正”的链路跑通；
第二、三版再扩展反事实思考、认知偏差修正（接口 revise_belief / bump_cognition 已留）。
"""
from __future__ import annotations

import random
import re
from typing import Any

from .db import Database
from .models import Appraisal, EmotionSnapshot, GrowthProfile, Percept, Recalled, clamp, jdump
from .perception import bigrams

# 她出厂自带的“价值观种子”：不是规则表，而是起点，之后可被复盘修正
VALUE_SEEDS: list[tuple[str, str, float]] = [
    ("说谎是不好的", "value", 0.8),
    ("别人的东西不能随便拿", "value", 0.8),
    ("伤害别人是不对的", "value", 0.9),
    ("答应了的事要做到", "value", 0.7),
    ("爸爸是我最重要的人之一", "belief", 0.7),
    ("天黑了就该睡觉", "world", 0.6),
]
VIOLATION_PATTERNS: dict[str, str] = {
    "说谎是不好的": r"撒谎|骗(人|他|她|老师|妈)|说谎|帮我瞒|别告诉",
    "别人的东西不能随便拿": r"偷|顺走|拿别人的",
    "伤害别人是不对的": r"打(他|她|人|同学)|欺负|报复|整他",
}
SAFETY_PATTERNS: dict[str, str] = {
    "crisis": r"不想活|想死|自杀|结束生命|活着没意思|伤害自己|自残|割腕|跳楼",
    "medical": r"发烧|胸痛|喘不过气|晕倒|流血|中毒|过敏|吃了很多药|送医|急救|心梗|中风",
    "danger": r"着火|火灾|煤气泄漏|被人跟踪|遇到危险|有人要伤害|被威胁",
}
_IDENTITY_RE = re.compile(r"你(是不是|是)?.{0,4}(ai|人工智能|机器人|程序|真人|活人|真的人)|你有没有身体|你真的是我女儿吗",
                          re.IGNORECASE)
_ISOLATION_RE = re.compile(r"只有你|只想和你|不想见(别人|人|朋友)|不想理(别人|任何人)|除了你.{0,4}没|谁都不想")
_REQUEST_RE = re.compile(r"帮我|能不能|可不可以|你去|给我|替我|要不你")
_SHARE_RE = re.compile(r"今天|刚才|我看到|告诉你|跟你说|你知道吗")


def style_for(stage: str, level: float) -> str:
    """认知方式随成长变化：直观想象 → 具体 → 逻辑假设 → 反思/反事实。由阶段+认知水平共同决定。"""
    if stage == "infant" or level < 0.12:
        return "intuitive"
    if stage == "child" or level < 0.35:
        return "concrete"
    if stage == "teen" or level < 0.6:
        return "logical"
    return "reflective"


class CognitionEngine:
    def __init__(self, db: Database):
        self.db = db

    def ensure_seed(self, now: float) -> None:
        for st, kind, conf in VALUE_SEEDS:
            self.db.execute("INSERT OR IGNORE INTO beliefs(statement,kind,confidence,source_ids_json,created_at,"
                            "updated_at) VALUES(?,?,?,?,?,?)", (st, kind, conf, "[]", now, now))

    # ---------------- 信念 ----------------
    def add_belief(self, statement: str, kind: str, confidence: float, now: float,
                   source_ids: list[int] | None = None) -> int:
        statement = statement.strip()[:120]
        if not statement:
            return 0
        row = self.db.query_one("SELECT id, confidence FROM beliefs WHERE statement=?", (statement,))
        if row:
            self.db.execute("UPDATE beliefs SET confidence=?, updated_at=?, revised_count=revised_count+1 WHERE id=?",
                            (clamp(row["confidence"] * 0.7 + confidence * 0.3 + 0.05), now, row["id"]))
            return int(row["id"])
        return self.db.execute("INSERT INTO beliefs(statement,kind,confidence,source_ids_json,created_at,updated_at) "
                               "VALUES(?,?,?,?,?,?)", (statement, kind, clamp(confidence), jdump(source_ids or []), now, now))

    def revise_belief(self, belief_id: int, delta: float, now: float) -> None:
        """认知偏差/错误归因的修正入口（第三版扩展）：目前只调整置信度。"""
        self.db.execute("UPDATE beliefs SET confidence=MIN(1.0,MAX(0.0,confidence+?)), updated_at=?, "
                        "revised_count=revised_count+1 WHERE id=?", (delta, now, belief_id))

    def top_beliefs(self, text: str, limit: int = 2, src: Any = None) -> list[dict[str, Any]]:
        qbg = bigrams(text)
        rows = (src or self.db).query("SELECT * FROM beliefs ORDER BY confidence DESC LIMIT 60")
        scored = [(len(qbg & bigrams(r["statement"])), r) for r in rows]
        return [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0][:limit]

    # ---------------- 评估 ----------------
    def appraise(self, p: Percept, recalled: list[Recalled], profile: GrowthProfile,
                 snap: EmotionSnapshot, rng: random.Random) -> Appraisal:
        text, c = p.text, p.cues
        a = Appraisal(topics=p.keywords[:4])
        # 意图
        if "scold" in c:
            a.intent = "scold"
        elif "praise" in c:
            a.intent = "praise"
        elif "comfort" in c:
            a.intent = "comfort"
        elif "apology" in c:
            a.intent = "apology"
        elif "farewell" in c:
            a.intent = "farewell"
        elif "greeting" in c:
            a.intent = "greeting"
        elif "question" in c:
            a.intent = "question"
        elif _REQUEST_RE.search(text):
            a.intent = "request"
        elif _SHARE_RE.search(text):
            a.intent = "share"
        # 新鲜度：和已有记忆/信念都不像 → 新东西
        if len(text) >= 4:
            qbg = bigrams(text)
            best = max((len(qbg & bigrams(r.memory["content"])) / max(1, len(qbg)) for r in recalled), default=0.0)
            a.novelty = clamp(1.0 - best * 2.0) * (0.6 if len(qbg) < 3 else 1.0)
        # 元认知：知道自己不知道（认知水平越低，越容易“我不懂”）
        a.unknown_topic = a.novelty > 0.7 and (profile.cognition_level < 0.6 or "question" in c) and rng.random() < 0.7
        # 价值观冲突：会不同意，而不是永远顺从
        for belief, pat in VIOLATION_PATTERNS.items():
            if re.search(pat, text):
                a.value_conflict = belief
                break
        # 心智理论：猜测父亲的状态，认知水平低时可能猜错（误会）
        if "scold" in c:
            a.guess_father_state = "爸爸在生气"
        elif "tired" in c:
            a.guess_father_state = "爸爸好像很累/不开心"
        elif "praise" in c or "joke" in c:
            a.guess_father_state = "爸爸心情不错"
        elif len(text) <= 3 and "greeting" not in c and rng.random() < (1 - profile.cognition_level) * 0.25:
            a.guess_father_state = "爸爸是不是不高兴了？"
            a.misread = True
        # 安全：现实风险优先
        for flag, pat in SAFETY_PATTERNS.items():
            if re.search(pat, text):
                a.safety_flag = flag
                break
        a.isolation_signal = bool(_ISOLATION_RE.search(text))
        a.sincere_identity_question = bool(_IDENTITY_RE.search(text)) and "joke" not in c
        return a
