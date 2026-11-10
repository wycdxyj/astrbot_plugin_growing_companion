"""记忆系统：不是精准数据库，而是“记得重要的事、淡忘琐碎的事、带着感情回忆”。

要点（与需求逐条对应）：
- 分层：episodic / semantic / emotional / relational（working 为内存滑动窗口 + conversation_log）
- 衰减与强化：effective_weight() 是纯函数；被召回、情绪强、关系重 → 衰减更慢
- 情绪加权 + 关联触发：检索分数含词面/情境标签/情绪一致性/权重，再沿关联边扩散；沉睡记忆只能被关联唤醒
- 主动浮现：未解决/情绪强/关系重的记忆在合适时可自己冒出来（surface）
- 重构：gist 可变、content 永不改写；每次改写留 revision
- 元记忆：clarity 低时注入“记不太清了”，而不是硬编
- 压缩：只影响注入（compressed 标记 + 语义记忆），原始记忆永不删除；强情绪/里程碑/未解决/承诺不可压缩
"""
from __future__ import annotations

import math
import random
from typing import Any, Iterable

from .clock import Clock, human_ago
from .db import Database
from .models import EMOTION_CN, EMOTIONS, EmotionSnapshot, Recalled, clamp, jdump, jload
from .perception import bigrams

DECAY_BY_TYPE = {"episodic": 0.10, "emotional": 0.05, "relational": 0.04, "semantic": 0.02}
DORMANT_WEIGHT = 0.08


# --------------------------------------------------------------------------- #
# 纯函数：行解析、衰减、权重
# --------------------------------------------------------------------------- #

def row_to_memory(r: dict[str, Any]) -> dict[str, Any]:
    m = dict(r)
    m["emotion"] = jload(r.get("emotion_json"), {}) or {}
    m["source_ids"] = jload(r.get("source_ids_json"), []) or []
    m["participants"] = jload(r.get("participants_json"), []) or []
    m["tags"] = jload(r.get("tags_json"), []) or []
    return m


def retention(m: dict[str, Any], now: float, speed: float = 1.0) -> float:
    """保留率（0~1）：时间越久越低；被召回、情绪强、关系重、受保护 → 稳定性高、衰减慢。"""
    age_days = max(0.0, now - float(m["last_touched_at"])) / 86400.0
    stability = (1.0 + 0.6 * m["recall_count"] + 3.0 * m["emotion_intensity"]
                 + (2.0 if m["protected"] else 0.0) + (1.0 if m["unresolved"] else 0.0)
                 + (1.5 if m["type"] == "relational" else 0.0))
    return math.exp(-m["decay_rate"] * speed * age_days / stability)


def effective_weight(m: dict[str, Any], now: float, speed: float = 1.0) -> float:
    base = m["importance"] * (1 + 0.8 * m["emotion_intensity"])
    floor = 0.35 * m["importance"] if m["protected"] else 0.0
    return clamp(max(base * retention(m, now, speed), floor))


def clarity_of(m: dict[str, Any], now: float, speed: float = 1.0) -> float:
    """元记忆：记得有多清楚。被改写过多次的记忆也会更模糊。"""
    c = retention(m, now, speed) * (1 - min(0.3, 0.05 * m.get("reconstruct_count", 0)))
    return clamp(c, 0.05, 1.0)


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def emotion_dot(a: dict[str, float], b: dict[str, float]) -> float:
    num = sum(a.get(e, 0) * b.get(e, 0) for e in EMOTIONS)
    na = math.sqrt(sum(a.get(e, 0) ** 2 for e in EMOTIONS))
    nb = math.sqrt(sum(b.get(e, 0) ** 2 for e in EMOTIONS))
    return num / (na * nb) if na and nb else 0.0


# 默认人格下各情绪的“平时水平”：信任平时就高(≈0.5)，按原始数值比它几乎永远最大，所以要看“比平时多出来多少”
DEFAULT_EMOTION_BASE: dict[str, float] = {"joy": 0.28, "trust": 0.50, "fear": 0.10, "sadness": 0.07,
                                          "curiosity": 0.30, "anger": 0.045}


def dominant_emotion(vec: dict[str, float], baseline: dict[str, float] | None = None) -> str:
    base = baseline or DEFAULT_EMOTION_BASE
    return max(EMOTIONS, key=lambda e: vec.get(e, 0) - base.get(e, 0))


def render_memory(m: dict[str, Any], now: float, vague: bool, width: int = 90) -> str:
    ago = human_ago(now - m["created_at"])
    inten = m.get("emotion_intensity", 0)
    emo = ""
    if inten >= 0.3 and m.get("emotion"):
        emo = "·" + EMOTION_CN[dominant_emotion(m["emotion"])]
    body = (m.get("gist") or m["content"]).replace("\n", " ")
    if m["type"] == "semantic":
        head = "[我的认知]"
    else:
        head = f"[{ago}{emo}]"
    if vague:
        return f"{head}（记不太清了）{body[:28]}…"
    return f"{head}{body[:width]}{'…' if len(body) > width else ''}"


# --------------------------------------------------------------------------- #
# 记忆系统
# --------------------------------------------------------------------------- #

class MemorySystem:
    def __init__(self, db: Database, clock: Clock, decay_speed: float = 1.0,
                 rng: random.Random | None = None):
        self.db = db
        self.clock = clock
        self.speed = max(0.05, decay_speed)
        self.rng = rng or random.Random()

    # ---------------- 写入 ----------------
    def add(self, *, mtype: str, content: str, now: float, snap: EmotionSnapshot | None = None,
            importance: float = 0.3, gist: str | None = None, participants: Iterable[str] = ("father",),
            tags: Iterable[str] = (), protected: bool | None = None, unresolved: bool = False,
            source_ids: Iterable[int] = (), decay_rate: float | None = None, link: bool = True,
            scope_id: str = 'legacy:father', source_kind: str = 'experience',
            confidence: float = 0.7) -> int:
        vec = {e: round(snap.values.get(e, 0.0), 3) for e in EMOTIONS} if snap else {}
        intensity = snap.intensity if snap else 0.0
        if protected is None:
            protected = intensity >= 0.55 or unresolved
        row = {
            "type": mtype, "content": content, "gist": gist if gist is not None else content,
            "created_at": now, "last_touched_at": now, "importance": clamp(importance),
            "emotion_json": jdump(vec), "valence": snap.valence if snap else 0.0,
            "arousal": snap.arousal if snap else 0.0, "emotion_intensity": intensity,
            "decay_rate": decay_rate if decay_rate is not None else DECAY_BY_TYPE.get(mtype, 0.1),
            "protected": int(bool(protected)), "unresolved": int(bool(unresolved)),
            "participants_json": jdump(list(participants)), "tags_json": jdump(list(tags)),
            "source_ids_json": jdump(list(source_ids)),
        }
        mem = row_to_memory({**row, "recall_count": 0, "reconstruct_count": 0})
        w = effective_weight(mem, now, self.speed)
        mid = self.db.execute(
            "INSERT INTO memories(type,content,gist,created_at,last_touched_at,importance,emotion_json,valence,"
            "arousal,emotion_intensity,weight,decay_rate,protected,unresolved,participants_json,tags_json,"
            "source_ids_json,clarity,scope_id,source_kind,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1.0,?,?,?)",
            (row["type"], row["content"], row["gist"], row["created_at"], row["last_touched_at"],
             row["importance"], row["emotion_json"], row["valence"], row["arousal"], row["emotion_intensity"],
             w, row["decay_rate"], row["protected"], row["unresolved"], row["participants_json"],
             row["tags_json"], row["source_ids_json"], scope_id, source_kind, clamp(confidence)))
        if link and mtype != "semantic":
            self._auto_link(mid, content, vec, now)
        return mid

    def _auto_link(self, mid: int, content: str, vec: dict[str, float], now: float) -> None:
        """新记忆与近期相似/同情绪/时间相邻的记忆建立关联边（关联触发的基础）。"""
        bg = bigrams(content)
        recent = [row_to_memory(r) for r in self.db.query(
            "SELECT * FROM memories WHERE id<? AND scope_id=(SELECT scope_id FROM memories WHERE id=?) "
            "AND type!='semantic' ORDER BY id DESC LIMIT 120", (mid, mid))]
        scored = sorted(((similarity(bg, bigrams(m["content"])), m) for m in recent), key=lambda x: -x[0])
        for sim, m in scored[:3]:
            if sim >= 0.15:
                self.link(mid, m["id"], "semantic", clamp(sim))
        if recent and now - recent[0]["created_at"] < 1800:
            self.link(recent[0]["id"], mid, "temporal", 0.3)
        if vec:
            same = [m for m in recent if m["emotion_intensity"] >= 0.4 and m["emotion"]
                    and emotion_dot(vec, m["emotion"]) > 0.9]
            for m in same[:1]:
                self.link(mid, m["id"], "emotional", 0.25)

    def link(self, a: int, b: int, kind: str, strength: float, bump: float = 0.0) -> None:
        if a == b:
            return
        if kind in ("semantic", "emotional", "context", "person"):
            a, b = min(a, b), max(a, b)
        now = self.clock.now()
        self.db.execute(
            "INSERT INTO memory_links(src,dst,kind,strength,created_at,last_activated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(src,dst,kind) DO UPDATE SET strength=MIN(1.0, MAX(strength, excluded.strength)+?), "
            "last_activated_at=excluded.last_activated_at",
            (a, b, kind, clamp(strength), now, now if bump else None, bump))

    # ---------------- 读取 ----------------
    def get(self, mid: int, src: Any = None) -> dict[str, Any] | None:
        r = (src or self.db).query_one("SELECT * FROM memories WHERE id=?", (mid,))
        return row_to_memory(r) if r else None

    def _candidates(self, text: str, keywords: list[str], scope_id: str) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for r in self.db.query("SELECT * FROM memories WHERE scope_id=? AND compressed=0 ORDER BY weight DESC LIMIT 400", (scope_id,)):
            out[r["id"]] = row_to_memory(r)
        for r in self.db.query("SELECT * FROM memories WHERE scope_id=? AND compressed=0 ORDER BY id DESC LIMIT 120", (scope_id,)):
            out.setdefault(r["id"], row_to_memory(r))
        # 沉睡/已压缩的旧记忆：只有词面命中时才会被“想起来”
        for kw in keywords[:3]:
            for r in self.db.query("SELECT * FROM memories WHERE scope_id=? AND (content LIKE ? OR gist LIKE ?) LIMIT 20",
                                   (scope_id, f"%{kw}%", f"%{kw}%")):
                out.setdefault(r["id"], row_to_memory(r))
        return out

    def retrieve(self, text: str, keywords: list[str], now: float, k: int,
                 snap: EmotionSnapshot | None = None, tags_now: Iterable[str] = (),
                 exclude_ids: Iterable[int] = (), scope_id: str = 'legacy:father') -> list[Recalled]:
        """检索 top-k：词面 + 情境标签 + 情绪一致性 + 权重 + 近因，再沿关联边扩散。不写库。"""
        if k <= 0:
            return []
        qbg = bigrams(text) | set(keywords)
        tags_set = set(tags_now)
        cur_vec = snap.values if snap else {}
        excl = set(exclude_ids)
        cand = self._candidates(text, keywords, scope_id)
        scored: dict[int, tuple[float, str]] = {}
        for mid, m in cand.items():
            if mid in excl:
                continue
            lex = similarity(qbg, bigrams(m["content"] + " " + (m["gist"] or "")))
            ctx = len(tags_set & set(m["tags"])) / max(1, len(tags_set | set(m["tags"])))
            emo = emotion_dot(cur_vec, m["emotion"]) if (cur_vec and m["emotion"]) else 0.0
            w = effective_weight(m, now, self.speed)
            rec = 0.5 / (1 + max(0.0, now - m["created_at"]) / 86400 / 7)
            score = 0.45 * lex + 0.15 * ctx + 0.12 * emo * (0.3 + m["emotion_intensity"]) + 0.20 * w + 0.05 * rec
            dormant = w < DORMANT_WEIGHT and not m["protected"]
            if dormant and lex < 0.35:
                continue                      # 沉睡的记忆，词面不够强就想不起来
            if lex < 0.08 and ctx < 0.34 and w < 0.45:
                continue                      # 完全不相关，不浮现
            if score >= 0.15:
                scored[mid] = (score, "awakened" if dormant else "direct")
        # 关联扩散：从最强的几个种子沿边把“沉睡/相关”的记忆带出来
        seeds = sorted(scored.items(), key=lambda kv: -kv[1][0])[:3]
        for sid, (sscore, _) in seeds:
            for e in self.db.query("SELECT src,dst,strength FROM memory_links WHERE (src=? OR dst=?) "
                                   "AND kind!='compress'", (sid, sid)):
                nid = e["dst"] if e["src"] == sid else e["src"]
                if nid in excl:
                    continue
                act = sscore * e["strength"] * 0.9          # 强关联边才能把邻居带出来；弱边扩散不动
                if act >= 0.10 and act > scored.get(nid, (0.0, ""))[0]:
                    m = cand.get(nid) or self.get(nid)
                    if m and m['scope_id'] == scope_id:
                        cand[nid] = m
                        w = effective_weight(m, now, self.speed)
                        scored[nid] = (act + 0.1 * w, "awakened" if w < DORMANT_WEIGHT else "assoc")
        out: list[Recalled] = []
        seen_parent: set[int] = set()
        for mid, (score, via) in sorted(scored.items(), key=lambda kv: -kv[1][0]):
            m = cand[mid]
            if m["compressed"] and m["compressed_into"]:      # 注入时优先用压缩后的语义认知
                pid = int(m["compressed_into"])
                if pid in seen_parent or pid in excl:
                    continue
                parent = self.get(pid)
                if not parent or parent['scope_id'] != scope_id:
                    continue
                seen_parent.add(pid)
                m = parent
                mid = pid
            if any(r.memory["id"] == mid for r in out):
                continue
            cl = clarity_of(m, now, self.speed)
            vague = cl < 0.4 and not m["protected"]
            out.append(Recalled(m, score, via, vague, render_memory(m, now, vague)))
            if len(out) >= k:
                break
        return out

    def reinforce(self, recalled: list[Recalled], now: float) -> None:
        """回忆即强化：召回次数+1、刷新衰减起点、解除沉睡；共同被想起的记忆彼此联结更紧。"""
        for r in recalled:
            m = r.memory
            m["recall_count"] += 1
            m["last_touched_at"] = now
            w = effective_weight(m, now, self.speed)
            self.db.execute("UPDATE memories SET recall_count=recall_count+1, last_recalled_at=?, "
                            "last_touched_at=?, weight=?, dormant=0, clarity=? WHERE id=?",
                            (now, now, w, clarity_of(m, now, self.speed), m["id"]))
        ids = [r.memory["id"] for r in recalled[:3]]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                self.link(ids[i], ids[j], "context", 0.1, bump=0.05)

    def surface(self, now: float, snap: EmotionSnapshot | None, rng: random.Random | None = None,
                energy: float = 0.7, cooldown_s: float = 6 * 3600,
                scope_id: str = 'legacy:father') -> Recalled | None:
        """主动浮现：未解决、情绪强、关系重的记忆，会在合适的时候自己冒出来。"""
        rng = rng or self.rng
        rows = [row_to_memory(r) for r in self.db.query(
            "SELECT * FROM memories WHERE scope_id=? AND compressed=0 AND (unresolved=1 OR emotion_intensity>=0.5 "
            "OR type='relational') ORDER BY weight DESC LIMIT 20", (scope_id,))]
        pool: list[tuple[float, dict[str, Any]]] = []
        for m in rows:
            if m["last_recalled_at"] and now - m["last_recalled_at"] < cooldown_s:
                continue
            if now - m["created_at"] < 1200:
                continue
            w = effective_weight(m, now, self.speed)
            cong = emotion_dot(snap.values, m["emotion"]) if (snap and m["emotion"]) else 0.0
            pool.append((w * (0.5 + m["emotion_intensity"]) * (1.0 + 0.8 * m["unresolved"]) * (0.7 + 0.5 * cong), m))
        if not pool:
            return None
        best = max(s for s, _ in pool)
        p = clamp(0.02 + 0.45 * best, 0.0, 0.35) * (0.5 + 0.5 * energy)
        if rng.random() > p:
            return None
        total = sum(s for s, _ in pool)
        x, acc = rng.random() * total, 0.0
        for s, m in pool:
            acc += s
            if x <= acc:
                vague = clarity_of(m, now, self.speed) < 0.4 and not m["protected"]
                return Recalled(m, s, "spontaneous", vague, render_memory(m, now, vague))
        return None

    # ---------------- 重构 ----------------
    def reconstruct(self, mid: int, new_gist: str, reason: str, now: float) -> bool:
        """记着记着理解变了：原文不动，只更新她的理解，并留下修订记录。"""
        m = self.get(mid)
        if not m or not new_gist.strip() or new_gist.strip() == (m["gist"] or "").strip():
            return False
        self.db.execute("INSERT INTO memory_revisions(memory_id,old_gist,new_gist,reason,created_at) "
                        "VALUES(?,?,?,?,?)", (mid, m["gist"], new_gist.strip(), reason, now))
        self.db.execute("UPDATE memories SET gist=?, reconstruct_count=reconstruct_count+1, last_touched_at=?, "
                        "consolidation=CASE WHEN consolidation='fresh' THEN 'reviewed' ELSE consolidation END "
                        "WHERE id=?", (new_gist.strip(), now, mid))
        return True

    def mark_reviewed(self, ids: Iterable[int], now: float) -> None:
        for i in ids:
            self.db.execute("UPDATE memories SET consolidation='reviewed', last_touched_at=? "
                            "WHERE id=? AND consolidation='fresh'", (now, i))

    # ---------------- 压缩（只影响注入） ----------------
    def find_compressible(self, now: float, min_age_s: float = 3 * 3600, min_group: int = 3,
                          max_group: int = 8) -> list[list[int]]:
        """找出可压缩的“重复同类、低情绪、低权重”的琐碎记忆簇。不可压缩类绝不入选。"""
        rows = [row_to_memory(r) for r in self.db.query(
            "SELECT * FROM memories WHERE scope_id='legacy:father' AND type IN ('episodic','emotional') AND compressed=0 AND protected=0 "
            "AND unresolved=0 AND emotion_intensity<0.3 AND created_at<? ORDER BY id", (now - min_age_s,))]
        rows = [m for m in rows if effective_weight(m, now, self.speed) < 0.4 and m["recall_count"] < 5]
        clusters: list[list[dict[str, Any]]] = []
        for m in rows:
            bg = bigrams(m["content"])
            for c in clusters:
                if len(c) < max_group and any(similarity(bg, bigrams(x["content"])) >= 0.2 for x in c):
                    c.append(m)
                    break
            else:
                clusters.append([m])
        return [[x["id"] for x in c] for c in clusters if len(c) >= min_group]

    @staticmethod
    def extractive_summary(rows: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for m in rows:
            for b in bigrams(m["content"]):
                counts[b] = counts.get(b, 0) + 1
        common = [b for b, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c >= 2][:4]
        topic = "、".join(common) if common else "日常的小事"
        return f"我和爸爸常常聊到这类日常（{topic}），一共 {len(rows)} 次左右，大多平平淡淡，具体哪天说了什么我记不太清了。"

    def compress_group(self, ids: list[int], summary: str, now: float) -> int | None:
        """把一簇琐碎情景压成一条语义认知。原始记忆保留，仅打上 compressed 标记。"""
        rows = [self.get(i) for i in ids]
        rows = [m for m in rows if m and not m["protected"] and not m["unresolved"] and not m["compressed"]
                and m["emotion_intensity"] < 0.55]
        if len(rows) < 2:
            return None
        if len({m['scope_id'] for m in rows}) != 1:
            return None  # 关联边/压缩均不能把不同群和私聊的内容混在一起。
        imp = max(m["importance"] for m in rows) * 0.9
        tags = sorted({t for m in rows for t in m["tags"]})[:6]
        sid = self.add(mtype="semantic", content=summary, gist=summary, now=now, importance=imp,
                       tags=tags, source_ids=[m["id"] for m in rows], protected=False, link=False,
                       scope_id=rows[0]['scope_id'])
        for m in rows:
            self.db.execute("UPDATE memories SET compressed=1, compressed_into=?, consolidation='consolidated' "
                            "WHERE id=?", (sid, m["id"]))
            self.link(sid, m["id"], "compress", 0.9)
        return sid

    # ---------------- tick：衰减刷新 ----------------
    def refresh_weights(self, now: float, limit: int = 5000) -> int:
        updates: list[tuple[float, float, int, int]] = []
        for r in self.db.query("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)):
            m = row_to_memory(r)
            w = effective_weight(m, now, self.speed)
            c = clarity_of(m, now, self.speed)
            dormant = int(w < DORMANT_WEIGHT and not m["protected"])
            if abs(w - r["weight"]) > 0.004 or abs(c - r["clarity"]) > 0.01 or dormant != r["dormant"]:
                updates.append((w, c, dormant, r["id"]))
        if updates:
            self.db.executemany("UPDATE memories SET weight=?, clarity=?, dormant=? WHERE id=?", updates)
        return len(updates)

    # ---------------- 统计 ----------------
    def stats(self, src: Any = None) -> dict[str, Any]:
        s = src or self.db
        by_type = {r["type"]: r["n"] for r in s.query("SELECT type, COUNT(*) n FROM memories GROUP BY type")}
        total = sum(by_type.values())
        comp = (s.query_one("SELECT COUNT(*) n FROM memories WHERE compressed=1") or {"n": 0})["n"]
        return {"total": total, "by_type": by_type, "compressed": comp}
