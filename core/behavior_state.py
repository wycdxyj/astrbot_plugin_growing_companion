"""表达经历：记录实际说过的话，而不是把想说的话当成已经说过。"""
from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from .db import Database
from .models import BehaviorPlan, jdump, jload

BEHAVIOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS behavior_state (
  scope_id TEXT NOT NULL, actor_id TEXT NOT NULL,
  intent TEXT NOT NULL DEFAULT 'observe',
  intent_confidence REAL NOT NULL DEFAULT 0 CHECK(intent_confidence BETWEEN 0 AND 1),
  expression_budget REAL NOT NULL DEFAULT 0 CHECK(expression_budget BETWEEN 0 AND 1),
  show_state_level REAL NOT NULL DEFAULT 0 CHECK(show_state_level BETWEEN 0 AND 1),
  reply_mode TEXT NOT NULL DEFAULT 'silence' CHECK(reply_mode IN ('single','multi','silence','short')),
  delay_seconds REAL NOT NULL DEFAULT 0 CHECK(delay_seconds>=0),
  split_count INTEGER NOT NULL DEFAULT 0 CHECK(split_count BETWEEN 0 AND 3),
  truncate_style TEXT NOT NULL DEFAULT 'none',
  persona_drift_score REAL NOT NULL DEFAULT 0 CHECK(persona_drift_score BETWEEN 0 AND 1),
  last_consistency_check REAL,
  fatigue_load REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
  PRIMARY KEY(scope_id, actor_id)
);
CREATE TABLE IF NOT EXISTS expression_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL, actor_id TEXT NOT NULL,
  created_at REAL NOT NULL, plan_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'planned', delivered_at REAL,
  delivered_text TEXT NOT NULL DEFAULT '', state_topics_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_expression_scope ON expression_history(scope_id,actor_id,id DESC);
CREATE TABLE IF NOT EXISTS expression_exposure (
  scope_id TEXT NOT NULL, actor_id TEXT NOT NULL, topic TEXT NOT NULL, spoken_at REAL NOT NULL,
  PRIMARY KEY(scope_id,actor_id,topic)
);
"""

PLAN_COLUMNS = ("intent", "intent_confidence", "expression_budget", "show_state_level", "reply_mode",
                "delay_seconds", "split_count", "truncate_style", "persona_drift_score", "last_consistency_check")


class BehaviorStore:
    def __init__(self, db: Database):
        self.db = db

    def history(self, scope: str, actor: str, now: float) -> dict[str, Any]:
        state = self.db.query_one("SELECT * FROM behavior_state WHERE scope_id=? AND actor_id=?", (scope, actor)) or {}
        rows = self.db.query("SELECT * FROM expression_history WHERE scope_id=? AND actor_id=? "
                             "AND delivered_at IS NOT NULL ORDER BY id DESC LIMIT 8", (scope, actor))
        last = {r['topic']: r['spoken_at'] for r in self.db.query(
            'SELECT topic,spoken_at FROM expression_exposure WHERE scope_id=? AND actor_id=?', (scope, actor))}
        # 同一主题的抑制不随重启消失；长间隔后自然恢复。
        return {"last_topics": last, "recent_texts": [r['delivered_text'] for r in rows[:4]],
                "fatigue_load": state.get('fatigue_load', 0) * math.exp(-max(0, now - state.get('updated_at', now)) / 900),
                "question_pressure": sum(math.exp(-max(0, now - r['delivered_at']) / 300)
                                         for r in rows if '?' in r['delivered_text'] or '？' in r['delivered_text'])}

    def record(self, scope: str, actor: str, plan: BehaviorPlan, now: float, fatigue_load: float) -> int:
        values = asdict(plan)
        cols = ','.join(PLAN_COLUMNS)
        placeholders = ','.join('?' for _ in PLAN_COLUMNS)
        updates = ','.join(f'{c}=excluded.{c}' for c in PLAN_COLUMNS)
        self.db.execute(f"INSERT INTO behavior_state(scope_id,actor_id,{cols},fatigue_load,updated_at) "
                        f"VALUES(?,?,{placeholders},?,?) ON CONFLICT(scope_id,actor_id) DO UPDATE SET "
                        f"{updates},fatigue_load=excluded.fatigue_load,updated_at=excluded.updated_at",
                        (scope, actor, *(values[c] for c in PLAN_COLUMNS), min(8, fatigue_load), now))
        return self.db.execute("INSERT INTO expression_history(scope_id,actor_id,created_at,plan_json,status) "
                               "VALUES(?,?,?,?,?)", (scope, actor, now, jdump(values), 'silent' if plan.silent else 'planned'))

    def complete(self, turn_id: int, parts: list[str], now: float, *, expected: int = 1) -> None:
        from .consistency import disclosed_topics
        text = '\n'.join(parts)
        status = 'sent' if parts and len(parts) >= expected else 'partial' if parts else 'unsent'
        topics = sorted(disclosed_topics(text))
        with self.db.transaction() as conn:
            row = conn.execute("SELECT scope_id,actor_id FROM expression_history WHERE id=? AND status='planned'",
                               (turn_id,)).fetchone()
            if not row:
                return
            conn.execute("UPDATE expression_history SET status=?,delivered_at=?,delivered_text=?,state_topics_json=? WHERE id=?",
                         (status, now if parts else None, text, jdump(topics), turn_id))
            for topic in topics:
                conn.execute('INSERT INTO expression_exposure VALUES(?,?,?,?) ON CONFLICT(scope_id,actor_id,topic) '
                             'DO UPDATE SET spoken_at=MAX(spoken_at,excluded.spoken_at)', (*row, topic, now))
