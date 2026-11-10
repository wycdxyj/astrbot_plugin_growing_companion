"""内在生活：偏好/审美、期待与愿望、承诺与约定、梦境与想象、小秘密。

MVP 为简单规则实现（数据结构齐全，行为很轻）：
- 偏好：由“提到某事物时她的情绪”慢慢形成，不是写死的
- 愿望：父亲说“带你去…/给你买…”会变成她的期待；被忘记会失落（第二版完善“承诺与约定”）
- 梦境：睡醒时按近期强情绪记忆的关键词拼出一点想象，是否分享由规划器决定
- 小秘密：表结构与写入口已留，由复盘在合适时写入
"""
from __future__ import annotations

import random
import re
from typing import Any

from .db import Database
from .models import EmotionSnapshot, Percept, clamp, jdump, jload

PREF_LEXICON: dict[str, list[str]] = {
    "颜色": ["红色", "蓝色", "粉色", "黄色", "绿色", "紫色", "白色"],
    "天气": ["下雨", "晴天", "下雪", "大风", "打雷", "彩虹"],
    "声音": ["钢琴", "雨声", "猫叫", "音乐", "风铃", "唱歌"],
    "食物": ["草莓", "蛋糕", "米饭", "牛奶", "苹果", "面条", "巧克力"],
    "事物": ["积木", "小熊", "画画", "故事书", "小猫", "小狗"],
}
_WISH_RE = re.compile(r"(带你去|给你买|陪你|教你|以后带你|等你长大)([^，。！？,.!?\n]{0,12})")
_DUE_RE = re.compile(r"明天|后天|周末|下次|以后|晚上|等你长大|下周|过年|暑假")


class InnerLife:
    def __init__(self, db: Database):
        self.db = db

    # ---------------- 偏好 ----------------
    def update_preferences(self, text: str, snap: EmotionSnapshot, now: float) -> list[str]:
        """提到某事物时的情绪效价，会慢慢沉淀成喜好/讨厌（证据越多越稳定）。"""
        touched: list[str] = []
        for cat, items in PREF_LEXICON.items():
            for it in items:
                if it not in text:
                    continue
                row = self.db.query_one("SELECT * FROM preferences WHERE category=? AND subject=?", (cat, it))
                v = clamp(snap.valence, -1, 1)
                if row:
                    n = row["evidence_count"] + 1
                    val = clamp(row["valence"] * 0.7 + v * 0.3, -1, 1)
                    self.db.execute("UPDATE preferences SET valence=?, strength=?, evidence_count=?, updated_at=? "
                                    "WHERE id=?", (val, clamp(0.1 + 0.08 * n), n, now, row["id"]))
                else:
                    self.db.execute("INSERT INTO preferences(category,subject,valence,strength,evidence_count,"
                                    "formed_at,updated_at) VALUES(?,?,?,?,?,?,?)", (cat, it, v, 0.18, 1, now, now))
                touched.append(it)
        return touched

    def top_preferences(self, limit: int = 3, src: Any = None) -> list[dict[str, Any]]:
        return (src or self.db).query(
            "SELECT * FROM preferences WHERE strength>=0.3 ORDER BY strength DESC LIMIT ?", (limit,))

    # ---------------- 愿望 / 承诺 ----------------
    def note_wishes_and_promises(self, p: Percept, memory_id: int | None, now: float) -> None:
        m = _WISH_RE.search(p.text)
        if m:
            content = f"想要{m.group(0)}"
            if not self.db.query_one("SELECT 1 FROM wishes WHERE content=? AND status='open'", (content,)):
                self.db.execute("INSERT INTO wishes(content,desire,status,created_at,last_felt_at,source_memory_id) "
                                "VALUES(?,?,?,?,?,?)", (content, 0.55, "open", now, now, memory_id))
        if "promise" in p.cues:
            due = _DUE_RE.search(p.text)
            self.db.execute("INSERT INTO promises(by_whom,content,due_hint,status,created_at,felt_strength,memory_id)"
                            " VALUES(?,?,?,?,?,?,?)",
                            ("father", p.text[:80], due.group(0) if due else "", "open", now, 0.6, memory_id))

    def open_promises(self, limit: int = 3, src: Any = None) -> list[dict[str, Any]]:
        return (src or self.db).query(
            "SELECT * FROM promises WHERE status='open' ORDER BY felt_strength DESC, id DESC LIMIT ?", (limit,))

    def open_wishes(self, limit: int = 3, src: Any = None) -> list[dict[str, Any]]:
        return (src or self.db).query(
            "SELECT * FROM wishes WHERE status='open' ORDER BY desire DESC, id DESC LIMIT ?", (limit,))

    # ---------------- 梦境 ----------------
    def maybe_dream(self, now: float, rng: random.Random, p: float = 0.35) -> int | None:
        """睡醒时可能带着一个梦：用近期强情绪记忆里的关键词拼出一点想象。"""
        if rng.random() > p:
            return None
        rows = self.db.query("SELECT id, tags_json, emotion_json FROM memories WHERE emotion_intensity>=0.25 "
                             "ORDER BY id DESC LIMIT 30")
        if len(rows) < 2:
            return None
        picks = rng.sample(rows, 2)
        kws: list[str] = []
        for r in picks:
            ks = [t[2:] for t in jload(r["tags_json"], []) if t.startswith("k:")]
            if ks:
                kws.append(rng.choice(ks))
        if len(kws) < 2:
            return None
        ending = rng.choice(["醒来的时候心里还暖暖的", "后来就找不到了，有点着急", "好像有点怕，又好像很好玩", "醒了就记不清后面了"])
        content = f"梦见{kws[0]}和{kws[1]}变成一起的了，{ending}"
        return self.db.execute("INSERT INTO dreams(content,emotion_json,source_ids_json,created_at,shared) "
                               "VALUES(?,?,?,?,0)", (content, "{}", jdump([r["id"] for r in picks]), now))

    def unshared_dream(self) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM dreams WHERE shared=0 ORDER BY id DESC LIMIT 1")

    def mark_dream_shared(self, dream_id: int) -> None:
        self.db.execute("UPDATE dreams SET shared=1 WHERE id=?", (dream_id,))

    # ---------------- 小秘密（接口） ----------------
    def add_secret(self, content: str, why_private: str, now: float) -> int:
        """小秘密：她知道什么不该随便说。MVP 只提供写入口，由复盘在“不好意思/自尊”场景调用。"""
        return self.db.execute("INSERT INTO secrets(content,why_private,created_at) VALUES(?,?,?)",
                               (content, why_private, now))

    def secret_count(self, src: Any = None) -> int:
        return ((src or self.db).query_one("SELECT COUNT(*) n FROM secrets WHERE revealed=0") or {"n": 0})["n"]
