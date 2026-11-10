"""SQLite 存储层。

- Database：读写连接，仅供引擎/tick/复盘使用
- ReadOnlyDB：只读连接（mode=ro + PRAGMA query_only），供查看面板与脑图服务使用。
  这样“只读不写”由数据库层强制保证，而不只是靠代码自觉。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .models import jload

# v3 为增量迁移；旧版原始记忆和表均保留。
SOCIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS social_relationships (
 actor_id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'stranger',
 familiarity REAL NOT NULL DEFAULT 0, trust REAL NOT NULL DEFAULT 0.2,
 interactions INTEGER NOT NULL DEFAULT 0, observations INTEGER NOT NULL DEFAULT 0,
 first_met_at REAL NOT NULL, last_seen_at REAL NOT NULL, last_interaction_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS social_cognition (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL, topic TEXT NOT NULL,
 kind TEXT NOT NULL, statement TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.3,
 source_memory_id INTEGER NOT NULL REFERENCES memories(id), source_actor TEXT NOT NULL,
 evidence_count INTEGER NOT NULL DEFAULT 1, needs_guidance INTEGER NOT NULL DEFAULT 0,
 discussion_state TEXT NOT NULL DEFAULT 'pending', offered_at REAL DEFAULT 0,
 father_view TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(scope_id,topic,statement));
CREATE TABLE IF NOT EXISTS speech_cooldowns (
 scope_id TEXT PRIMARY KEY, next_allowed_at REAL NOT NULL DEFAULT 0,
 lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
 last_spoke_at REAL NOT NULL DEFAULT 0, awaiting_ack INTEGER NOT NULL DEFAULT 0,
 unanswered INTEGER NOT NULL DEFAULT 0, frustration REAL NOT NULL DEFAULT 0,
 window_start REAL NOT NULL DEFAULT 0, window_count INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS seen_messages (
 scope_id TEXT NOT NULL, message_id TEXT NOT NULL, seen_at REAL NOT NULL,
 PRIMARY KEY(scope_id,message_id));
CREATE INDEX IF NOT EXISTS idx_social_pending ON social_cognition(discussion_state,needs_guidance);
CREATE TABLE IF NOT EXISTS memory_embeddings (
 memory_id INTEGER NOT NULL REFERENCES memories(id), model TEXT NOT NULL, dimensions INTEGER NOT NULL,
 embedding BLOB NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(memory_id,model));
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- ===================== 人格 / 真人感 =====================
-- 人格底色：连续数值，随成长缓慢漂移（initial 保留出厂值，限制漂移幅度）
CREATE TABLE IF NOT EXISTS persona_traits (
  key TEXT PRIMARY KEY,            -- introversion/sensitivity/patience/clinginess/humor/stubbornness/openness
  value REAL NOT NULL, initial REAL NOT NULL, updated_at REAL NOT NULL);
-- 语言风格：口癖、称呼、语气词、生气/开心时的表达差异、模仿来的说法
CREATE TABLE IF NOT EXISTS persona_style (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
-- 审美与偏好：颜色/声音/天气/食物……由经历形成，不是写死的
CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, subject TEXT NOT NULL,
  valence REAL NOT NULL DEFAULT 0, strength REAL NOT NULL DEFAULT 0.1,
  evidence_count INTEGER NOT NULL DEFAULT 1, formed_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(category, subject));
-- 身体感
CREATE TABLE IF NOT EXISTS body_state (
  id INTEGER PRIMARY KEY CHECK(id=1), energy REAL, sleepiness REAL, phase TEXT,
  groggy_until REAL DEFAULT 0, woken_by_user INTEGER DEFAULT 0, updated_at REAL);
-- 注意力与社交需求
CREATE TABLE IF NOT EXISTS attention_state (
  id INTEGER PRIMARY KEY CHECK(id=1), focus REAL, away_until REAL DEFAULT 0,
  away_reason TEXT DEFAULT '', social_need REAL, solitude_need REAL, updated_at REAL);
-- 时间感
CREATE TABLE IF NOT EXISTS time_sense (
  id INTEGER PRIMARY KEY CHECK(id=1), born_at REAL, first_chat_at REAL, last_contact_at REAL,
  last_tick_at REAL, last_gap_s REAL, birthday_md TEXT);
-- 情绪：6 维连续值 + 基线 + 惯性
CREATE TABLE IF NOT EXISTS emotion_state (
  emotion TEXT PRIMARY KEY, value REAL NOT NULL, baseline REAL NOT NULL,
  inertia REAL NOT NULL, updated_at REAL NOT NULL);
-- 情绪矛盾心理：被压住的情绪（嘴硬）、事后后悔
CREATE TABLE IF NOT EXISTS emotion_meta (
  id INTEGER PRIMARY KEY CHECK(id=1), suppressed TEXT DEFAULT '', regret REAL DEFAULT 0,
  last_peak_anger REAL DEFAULT 0, updated_at REAL);
-- 期待与愿望
CREATE TABLE IF NOT EXISTS wishes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, desire REAL NOT NULL DEFAULT 0.5,
  status TEXT NOT NULL DEFAULT 'open',   -- open/fulfilled/lost
  created_at REAL NOT NULL, last_felt_at REAL, source_memory_id INTEGER);
-- 梦境与想象
CREATE TABLE IF NOT EXISTS dreams (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, emotion_json TEXT DEFAULT '{}',
  source_ids_json TEXT DEFAULT '[]', created_at REAL NOT NULL, shared INTEGER NOT NULL DEFAULT 0);
-- 小秘密与隐私边界
CREATE TABLE IF NOT EXISTS secrets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, why_private TEXT DEFAULT '',
  created_at REAL NOT NULL, revealed INTEGER NOT NULL DEFAULT 0);
-- 承诺与约定
CREATE TABLE IF NOT EXISTS promises (
  id INTEGER PRIMARY KEY AUTOINCREMENT, by_whom TEXT NOT NULL, content TEXT NOT NULL,
  due_hint TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'open',  -- open/kept/broken/forgotten
  created_at REAL NOT NULL, felt_strength REAL NOT NULL DEFAULT 0.5, memory_id INTEGER);

-- ===================== 记忆 =====================
CREATE TABLE IF NOT EXISTS memories (
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
);
CREATE INDEX IF NOT EXISTS idx_mem_weight ON memories(weight DESC);
CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(type);

CREATE TABLE IF NOT EXISTS memory_links (
  src INTEGER NOT NULL, dst INTEGER NOT NULL, kind TEXT NOT NULL,  -- semantic/emotional/temporal/person/context/compress
  strength REAL NOT NULL DEFAULT 0.2, created_at REAL NOT NULL, last_activated_at REAL,
  PRIMARY KEY (src, dst, kind));
CREATE INDEX IF NOT EXISTS idx_link_src ON memory_links(src);
CREATE INDEX IF NOT EXISTS idx_link_dst ON memory_links(dst);

-- 记忆重构历史（“记着记着理解变了”可回溯）
CREATE TABLE IF NOT EXISTS memory_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL, old_gist TEXT, new_gist TEXT,
  reason TEXT, created_at REAL NOT NULL);

-- ===================== 成长 =====================
CREATE TABLE IF NOT EXISTS growth_state (
  id INTEGER PRIMARY KEY CHECK(id=1), stage TEXT NOT NULL, stage_since REAL NOT NULL,
  cognition_level REAL NOT NULL DEFAULT 0.05, total_experience INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL);
-- 能力池定义（解锁条件）：到阶段 + 经历够 + 练习够 + 认知够 + 状态支持
CREATE TABLE IF NOT EXISTS capability_defs (
  key TEXT PRIMARY KEY, domain TEXT NOT NULL, title TEXT NOT NULL, stage_min TEXT NOT NULL,
  req_experience INTEGER NOT NULL, req_practice INTEGER NOT NULL, req_cognition REAL NOT NULL,
  min_energy REAL NOT NULL, unlock_threshold REAL NOT NULL, prereq_json TEXT NOT NULL DEFAULT '{}');
-- 能力熟练度（0~1 渐进）
CREATE TABLE IF NOT EXISTS capabilities (
  key TEXT PRIMARY KEY, proficiency REAL NOT NULL DEFAULT 0, peak REAL NOT NULL DEFAULT 0,
  practice_count INTEGER NOT NULL DEFAULT 0, experience_count INTEGER NOT NULL DEFAULT 0,
  unlock_state TEXT NOT NULL DEFAULT 'locked',   -- locked/emerging/unlocked/mastered
  first_unlocked_at REAL, last_practiced_at REAL, regress_count INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS milestones (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT DEFAULT '', stage TEXT, memory_id INTEGER, created_at REAL NOT NULL,
  UNIQUE(kind, title));

-- ===================== 关系 =====================
CREATE TABLE IF NOT EXISTS relationships (
  person_id TEXT PRIMARY KEY, role TEXT NOT NULL, trust REAL NOT NULL, attachment REAL NOT NULL,
  security REAL NOT NULL DEFAULT 0.5, longing REAL NOT NULL DEFAULT 0, last_contact_at REAL,
  shared_memory_count INTEGER NOT NULL DEFAULT 0, conflict_open INTEGER NOT NULL DEFAULT 0,
  last_change_note TEXT DEFAULT '', first_met_at REAL);
CREATE TABLE IF NOT EXISTS relationship_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, person_id TEXT NOT NULL, kind TEXT NOT NULL,
  d_trust REAL NOT NULL DEFAULT 0, d_attachment REAL NOT NULL DEFAULT 0, note TEXT DEFAULT '',
  created_at REAL NOT NULL);

-- ===================== 认知 / 复盘 / 对话 / 监控 =====================
CREATE TABLE IF NOT EXISTS beliefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, statement TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'belief',   -- belief/causal/value/world
  confidence REAL NOT NULL DEFAULT 0.5, source_ids_json TEXT DEFAULT '[]',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, revised_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reflections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, depth INTEGER NOT NULL,
  pull REAL NOT NULL, trigger_json TEXT DEFAULT '[]', summary TEXT NOT NULL, learned TEXT DEFAULT '',
  memory_ids_json TEXT DEFAULT '[]', diary INTEGER NOT NULL DEFAULT 0,
  share_intent REAL NOT NULL DEFAULT 0, shared_with_father INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_log(user_id, id DESC);
CREATE TABLE IF NOT EXISTS token_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, fixed INTEGER, state INTEGER,
  retrieval INTEGER, dialog INTEGER, total INTEGER, budget INTEGER, fixed_cache_hit INTEGER);
"""


class Database:
    """读写数据库。单连接 + 线程锁；数据量小，直接同步调用即可。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate_v3()
            self.conn.commit()

    def _migrate_v3(self) -> None:
        """兼容 v0.1.3 的幂等增量迁移，不覆盖原文、不重新绑定父亲。"""
        columns = {
            "memories": {"scope_id": "TEXT NOT NULL DEFAULT 'legacy:father'",
                         "source_kind": "TEXT NOT NULL DEFAULT 'experience'",
                         "confidence": "REAL NOT NULL DEFAULT 0.7"},
            "conversation_log": {"scope_id": "TEXT NOT NULL DEFAULT 'legacy:father'"},
            "reflections": {"scope_id": "TEXT NOT NULL DEFAULT 'legacy:father'"},
        }
        for table, additions in columns.items():
            present = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in additions.items():
                if name not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.conn.executescript(SOCIAL_SCHEMA)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memories(scope_id,id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_dialog_scope ON conversation_log(scope_id,id)")
        self.conn.execute("PRAGMA user_version=3")

    @contextmanager
    def transaction(self):
        """短事务用于跨事件/进程抢占发言名额；事务内不允许 await。"""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    # ---- 写 ----
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, [tuple(r) for r in rows])
            self.conn.commit()

    # ---- 读 ----
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- kv ----
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT v FROM kv WHERE k=?", (key,))
        return jload(row["v"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        from .models import jdump
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (key, jdump(value)))

    @property
    def total_changes(self) -> int:
        return self.conn.total_changes

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass


class ReadOnlyDB:
    """只读连接：数据库层面禁止写（mode=ro + query_only）。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT v FROM kv WHERE k=?", (key,))
        return jload(row["v"], default) if row else default

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
