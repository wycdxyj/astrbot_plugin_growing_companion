PRAGMA foreign_keys=ON;

CREATE TABLE attention_state (
  id INTEGER PRIMARY KEY CHECK(id=1), focus REAL, away_until REAL DEFAULT 0,
  away_reason TEXT DEFAULT '', social_need REAL, solitude_need REAL, updated_at REAL);

CREATE TABLE beliefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, statement TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'belief',   -- belief/causal/value/world
  confidence REAL NOT NULL DEFAULT 0.5, source_ids_json TEXT DEFAULT '[]',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, revised_count INTEGER NOT NULL DEFAULT 0);

CREATE TABLE body_state (
  id INTEGER PRIMARY KEY CHECK(id=1), energy REAL, sleepiness REAL, phase TEXT,
  groggy_until REAL DEFAULT 0, woken_by_user INTEGER DEFAULT 0, updated_at REAL);

CREATE TABLE capabilities (
  key TEXT PRIMARY KEY, proficiency REAL NOT NULL DEFAULT 0, peak REAL NOT NULL DEFAULT 0,
  practice_count INTEGER NOT NULL DEFAULT 0, experience_count INTEGER NOT NULL DEFAULT 0,
  unlock_state TEXT NOT NULL DEFAULT 'locked',   -- locked/emerging/unlocked/mastered
  first_unlocked_at REAL, last_practiced_at REAL, regress_count INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL);

CREATE TABLE capability_defs (
  key TEXT PRIMARY KEY, domain TEXT NOT NULL, title TEXT NOT NULL, stage_min TEXT NOT NULL,
  req_experience INTEGER NOT NULL, req_practice INTEGER NOT NULL, req_cognition REAL NOT NULL,
  min_energy REAL NOT NULL, unlock_threshold REAL NOT NULL, prereq_json TEXT NOT NULL DEFAULT '{}');

CREATE TABLE conversation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, scope_id TEXT NOT NULL DEFAULT 'legacy:father');

CREATE TABLE dreams (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, emotion_json TEXT DEFAULT '{}',
  source_ids_json TEXT DEFAULT '[]', created_at REAL NOT NULL, shared INTEGER NOT NULL DEFAULT 0);

CREATE TABLE emotion_meta (
  id INTEGER PRIMARY KEY CHECK(id=1), suppressed TEXT DEFAULT '', regret REAL DEFAULT 0,
  last_peak_anger REAL DEFAULT 0, updated_at REAL);

CREATE TABLE emotion_state (
  emotion TEXT PRIMARY KEY, value REAL NOT NULL, baseline REAL NOT NULL,
  inertia REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE growth_state (
  id INTEGER PRIMARY KEY CHECK(id=1), stage TEXT NOT NULL, stage_since REAL NOT NULL,
  cognition_level REAL NOT NULL DEFAULT 0.05, total_experience INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL);

CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,                      -- episodic/semantic/emotional/relational（working 在内存窗口+conversation_log）
  content TEXT NOT NULL,                   -- 原文：永不改写、永不删除
  gist TEXT NOT NULL DEFAULT '',           -- 她当前的理解：可被重构（reconstruct）
  created_at REAL NOT NULL,
  last_touched_at REAL NOT NULL,           -- 衰减的时间起点（被回忆/复盘会刷新）
  last_recalled_at REAL,
  recall_count INTEGER NOT NULL DEFAULT 0,
  importance REAL NOT NULL DEFAULT 0.3,
  emotion_json TEXT NOT NULL DEFAULT '{}', -- 6 维情绪向量
  valence REAL NOT NULL DEFAULT 0,         -- 情绪坐标：效价
  arousal REAL NOT NULL DEFAULT 0,         -- 情绪坐标：唤醒度
  emotion_intensity REAL NOT NULL DEFAULT 0,
  weight REAL NOT NULL DEFAULT 0.3,        -- 当前有效权重（tick 刷新；查看面板另行纯计算）
  decay_rate REAL NOT NULL DEFAULT 0.08,
  clarity REAL NOT NULL DEFAULT 1.0,       -- 元记忆：记得有多清楚
  consolidation TEXT NOT NULL DEFAULT 'fresh',  -- fresh / reviewed / consolidated
  compressed INTEGER NOT NULL DEFAULT 0,   -- 是否已被压缩进某条语义记忆（仅影响注入，不影响存储）
  compressed_into INTEGER,                 -- 压缩后的语义记忆 id
  source_ids_json TEXT NOT NULL DEFAULT '[]',   -- 语义记忆指向的原始记忆
  protected INTEGER NOT NULL DEFAULT 0,    -- 不可压缩：强情绪/里程碑/未解决/承诺
  unresolved INTEGER NOT NULL DEFAULT 0,
  participants_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',    -- 情境/情绪/人物/关键词标签，用于关联触发
  reconstruct_count INTEGER NOT NULL DEFAULT 0,
  dormant INTEGER NOT NULL DEFAULT 0       -- 沉睡：不参与常规检索，但可被关联唤醒
, scope_id TEXT NOT NULL DEFAULT 'legacy:father', source_kind TEXT NOT NULL DEFAULT 'experience', confidence REAL NOT NULL DEFAULT 0.7);

CREATE TABLE memory_embeddings (
 memory_id INTEGER NOT NULL REFERENCES memories(id), model TEXT NOT NULL, dimensions INTEGER NOT NULL,
 embedding BLOB NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(memory_id,model));

CREATE TABLE memory_links (
  src INTEGER NOT NULL, dst INTEGER NOT NULL, kind TEXT NOT NULL,  -- semantic/emotional/temporal/person/context/compress
  strength REAL NOT NULL DEFAULT 0.2, created_at REAL NOT NULL, last_activated_at REAL,
  PRIMARY KEY (src, dst, kind));

CREATE TABLE memory_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL, old_gist TEXT, new_gist TEXT,
  reason TEXT, created_at REAL NOT NULL);

CREATE TABLE milestones (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT DEFAULT '', stage TEXT, memory_id INTEGER, created_at REAL NOT NULL,
  UNIQUE(kind, title));

CREATE TABLE persona_style (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE persona_traits (
  key TEXT PRIMARY KEY,            -- introversion/sensitivity/patience/clinginess/humor/stubbornness/openness
  value REAL NOT NULL, initial REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE preferences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, subject TEXT NOT NULL,
  valence REAL NOT NULL DEFAULT 0, strength REAL NOT NULL DEFAULT 0.1,
  evidence_count INTEGER NOT NULL DEFAULT 1, formed_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(category, subject));

CREATE TABLE promises (
  id INTEGER PRIMARY KEY AUTOINCREMENT, by_whom TEXT NOT NULL, content TEXT NOT NULL,
  due_hint TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'open',  -- open/kept/broken/forgotten
  created_at REAL NOT NULL, felt_strength REAL NOT NULL DEFAULT 0.5, memory_id INTEGER);

CREATE TABLE reflections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, depth INTEGER NOT NULL,
  pull REAL NOT NULL, trigger_json TEXT DEFAULT '[]', summary TEXT NOT NULL, learned TEXT DEFAULT '',
  memory_ids_json TEXT DEFAULT '[]', diary INTEGER NOT NULL DEFAULT 0,
  share_intent REAL NOT NULL DEFAULT 0, shared_with_father INTEGER NOT NULL DEFAULT 0, scope_id TEXT NOT NULL DEFAULT 'legacy:father');

CREATE TABLE relationship_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL, kind TEXT NOT NULL,
  d_trust REAL NOT NULL DEFAULT 0, d_attachment REAL NOT NULL DEFAULT 0, note TEXT DEFAULT '',
  created_at REAL NOT NULL);

CREATE TABLE relationships (
  person_id TEXT PRIMARY KEY, role TEXT NOT NULL, trust REAL NOT NULL, attachment REAL NOT NULL,
  security REAL NOT NULL DEFAULT 0.5, longing REAL NOT NULL DEFAULT 0, last_contact_at REAL,
  shared_memory_count INTEGER NOT NULL DEFAULT 0, conflict_open INTEGER NOT NULL DEFAULT 0,
  last_change_note TEXT DEFAULT '', first_met_at REAL);

CREATE TABLE secrets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, why_private TEXT DEFAULT '',
  created_at REAL NOT NULL, revealed INTEGER NOT NULL DEFAULT 0);

CREATE TABLE seen_messages (
 scope_id TEXT NOT NULL, message_id TEXT NOT NULL, seen_at REAL NOT NULL,
 PRIMARY KEY(scope_id,message_id));

CREATE TABLE social_cognition (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL, topic TEXT NOT NULL,
 kind TEXT NOT NULL, statement TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.3,
 source_memory_id INTEGER NOT NULL REFERENCES memories(id), source_actor TEXT NOT NULL,
 evidence_count INTEGER NOT NULL DEFAULT 1, needs_guidance INTEGER NOT NULL DEFAULT 0,
 discussion_state TEXT NOT NULL DEFAULT 'pending', offered_at REAL DEFAULT 0,
 father_view TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(scope_id,topic,statement));

CREATE TABLE social_relationships (
 actor_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'stranger',
 familiarity REAL NOT NULL DEFAULT 0, trust REAL NOT NULL DEFAULT 0.2,
 interactions INTEGER NOT NULL DEFAULT 0, observations INTEGER NOT NULL DEFAULT 0,
 first_met_at REAL NOT NULL, last_seen_at REAL NOT NULL, last_interaction_at REAL DEFAULT 0);

CREATE TABLE speech_cooldowns (
 scope_id TEXT PRIMARY KEY, next_allowed_at REAL NOT NULL DEFAULT 0,
 lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
 last_spoke_at REAL NOT NULL DEFAULT 0, awaiting_ack INTEGER NOT NULL DEFAULT 0,
 unanswered INTEGER NOT NULL DEFAULT 0, frustration REAL NOT NULL DEFAULT 0,
 window_start REAL NOT NULL DEFAULT 0, window_count INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT 0);

CREATE TABLE time_sense (
  id INTEGER PRIMARY KEY CHECK(id=1), born_at REAL, first_chat_at REAL, last_contact_at REAL,
  last_tick_at REAL, last_gap_s REAL, birthday_md TEXT);

CREATE TABLE token_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, fixed INTEGER, state INTEGER,
  retrieval INTEGER, dialog INTEGER, total INTEGER, budget INTEGER, fixed_cache_hit INTEGER);

CREATE TABLE wishes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, desire REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'open',   -- open/fulfilled/lost
  created_at REAL NOT NULL, last_felt_at REAL, source_memory_id INTEGER);

CREATE INDEX idx_conv_user ON conversation_log(user_id, id DESC);

CREATE INDEX idx_dialog_scope ON conversation_log(scope_id,id);

CREATE INDEX idx_link_dst ON memory_links(dst);

CREATE INDEX idx_link_src ON memory_links(src);

CREATE INDEX idx_mem_created ON memories(created_at DESC);

CREATE INDEX idx_mem_type ON memories(type);

CREATE INDEX idx_mem_weight ON memories(weight DESC);

CREATE INDEX idx_memory_scope ON memories(scope_id,id);

CREATE INDEX idx_social_pending ON social_cognition(discussion_state,needs_guidance);

PRAGMA user_version=3;
