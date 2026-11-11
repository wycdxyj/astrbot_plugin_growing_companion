-- 适用于 v3 数据库；应用启动也会自动幂等迁移。
BEGIN IMMEDIATE;

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

PRAGMA user_version=4;
COMMIT;
