"""关系系统：父亲是核心依恋对象，但关系模型本身支持多人（陌生人/熟人），不鼓励孤立。

信任“难建易毁、修复慢”；想念（longing）随久别而生，重逢时强化依恋；
冲突后需要修复（conflict_open），不是固定数值。
"""
from __future__ import annotations

import math
from typing import Any

from .db import Database
from .models import Relationship, clamp

# (d_trust, d_attachment)：损失大于获得，体现“信任难建易毁”
EVENT_EFFECTS: dict[str, tuple[float, float]] = {
    "praise": (0.010, 0.008),
    "comfort": (0.015, 0.010),
    "scold": (-0.022, -0.004),
    "apology": (0.015, 0.006),       # 修复比破坏慢
    "kept_promise": (0.030, 0.010),
    "broken_promise": (-0.040, -0.006),
    "chat": (0.001, 0.002),
    "reunion": (0.0, 0.020),
}


class RelationshipSystem:
    def __init__(self, db: Database):
        self.db = db

    def ensure(self, person_id: str, role: str, now: float) -> None:
        if self.db.query_one("SELECT 1 FROM relationships WHERE person_id=?", (person_id,)):
            return
        trust, attach = (0.55, 0.55) if role == "father" else (0.2, 0.1)
        self.db.execute("INSERT INTO relationships(person_id,role,trust,attachment,security,longing,"
                        "last_contact_at,shared_memory_count,conflict_open,last_change_note,first_met_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (person_id, role, trust, attach, 0.5, 0.0, None, 0, 0, "初次见面", now))

    @staticmethod
    def _row(r: dict[str, Any]) -> Relationship:
        return Relationship(r["person_id"], r["role"], r["trust"], r["attachment"], r["security"],
                            r["longing"], r["last_contact_at"] or 0.0, r["shared_memory_count"],
                            bool(r["conflict_open"]), r["last_change_note"] or "", r["first_met_at"] or 0.0)

    def get(self, person_id: str, src: Any = None) -> Relationship | None:
        r = (src or self.db).query_one("SELECT * FROM relationships WHERE person_id=?", (person_id,))
        return self._row(r) if r else None

    def father(self, src: Any = None) -> Relationship | None:
        r = (src or self.db).query_one("SELECT * FROM relationships WHERE role='father' LIMIT 1")
        return self._row(r) if r else None

    def save(self, rel: Relationship) -> None:
        self.db.execute(
            "UPDATE relationships SET trust=?, attachment=?, security=?, longing=?, last_contact_at=?, "
            "shared_memory_count=?, conflict_open=?, last_change_note=? WHERE person_id=?",
            (clamp(rel.trust), clamp(rel.attachment), clamp(rel.security), clamp(rel.longing),
             rel.last_contact_at, rel.shared_memory_count, int(rel.conflict_open),
             rel.last_change_note, rel.person_id))

    def recent_events(self, person_id: str, limit: int = 5, src: Any = None) -> list[dict[str, Any]]:
        return (src or self.db).query(
            "SELECT * FROM relationship_events WHERE person_id=? ORDER BY id DESC LIMIT ?", (person_id, limit))

    # ---- 事件 ----
    def apply_event(self, rel: Relationship, kind: str, now: float, note: str = "") -> list[str]:
        """应用关系事件，返回触发的“第一次”标记（如 first_conflict / first_reconcile）。"""
        flags: list[str] = []
        dt, da = EVENT_EFFECTS.get(kind, (0.0, 0.0))
        if dt == 0 and da == 0:
            return flags
        rel.trust = clamp(rel.trust + dt)
        rel.attachment = clamp(rel.attachment + da)
        if kind == "scold":
            if not self.db.query_one("SELECT 1 FROM relationship_events WHERE person_id=? AND kind='scold'",
                                     (rel.person_id,)):
                flags.append("first_conflict")
            rel.conflict_open = True
            rel.security = clamp(rel.security - 0.03)
        elif kind == "apology" and rel.conflict_open:
            rel.conflict_open = False
            rel.security = clamp(rel.security + 0.04)
            if not self.db.query_one("SELECT 1 FROM relationship_events WHERE person_id=? AND kind='apology' "
                                     "AND note='reconcile'", (rel.person_id,)):
                flags.append("first_reconcile")
            note = "reconcile"
        elif kind in ("praise", "comfort", "kept_promise"):
            rel.security = clamp(rel.security + 0.01)
        rel.last_change_note = {
            "praise": "被夸了，心里暖暖的", "comfort": "被安慰了，更信任一点",
            "scold": "被凶了，有点受伤", "apology": "道歉了，关系在慢慢修复",
            "kept_promise": "说到做到了", "broken_promise": "答应的事没做到，失落",
            "reunion": "久别重逢，更想靠近", "chat": rel.last_change_note,
        }.get(kind, rel.last_change_note)
        self.db.execute("INSERT INTO relationship_events(person_id,kind,d_trust,d_attachment,note,created_at) "
                        "VALUES(?,?,?,?,?,?)", (rel.person_id, kind, dt, da, note, now))
        return flags

    # ---- 时间流逝 ----
    @staticmethod
    def update_longing(rel: Relationship, now: float, clinginess: float) -> Relationship:
        """想念：随久别增长，依恋越深、越黏人越想；久到一定程度会变淡（但依恋底子还在）。"""
        if not rel.last_contact_at:
            return rel
        hours = max(0.0, now - rel.last_contact_at) / 3600.0
        ramp = 1 - math.exp(-hours / 36.0)
        fade = math.exp(-max(0.0, hours - 24 * 14) / (24 * 30.0))
        rel.longing = clamp(ramp * fade * (0.35 + 0.65 * rel.attachment) * (0.6 + 0.8 * clinginess))
        if hours > 24 * 14:   # 太久不联系，依恋极缓慢淡化
            rel.attachment = clamp(rel.attachment - 0.0005 * min(hours / 24 - 14, 60) / 60)
        return rel

    def on_contact(self, rel: Relationship, now: float) -> tuple[Relationship, bool]:
        """收到消息：返回 (关系, 是否久别重逢)。重逢会强化依恋，想念随之消解。"""
        reunion = bool(rel.last_contact_at and now - rel.last_contact_at > 6 * 3600 and rel.longing > 0.25)
        if reunion:
            self.apply_event(rel, "reunion", now, "reunion")
        rel.last_contact_at = now
        rel.longing = clamp(rel.longing * 0.3)
        return rel, reunion
