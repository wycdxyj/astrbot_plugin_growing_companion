"""只读查看面板：能看，不能改。了解她，不是操控她。

- 所有函数只接收 ReadOnlyDB（mode=ro + query_only，数据库层面不可写）
- 不触发任何状态变化：不做“回忆强化”、不刷新权重；当前情绪/身体/想念都是“纯计算推算到现在”
- 数据不存在时返回友好提示，不报错
"""
from __future__ import annotations

from typing import Any

from .clock import Clock, human_ago
from .db import ReadOnlyDB
from .emotion import EmotionEngine
from .growth import CAP_POOL, GrowthEngine
from .memory import dominant_emotion, effective_weight, row_to_memory
from .models import EMOTION_CN, EMOTIONS, MEMORY_TYPE_CN, STAGE_CN, jload
from .persona import AttentionModel, BodyModel, PersonaStore
from .reflection import ReflectionEngine
from .relationship import RelationshipSystem

PHASE_CN = {"awake": "清醒", "drowsy": "犯困", "asleep": "在睡觉", "groggy": "迷迷糊糊（刚睡醒/被吵醒）"}
STATE_CN = {"locked": "未开始", "emerging": "还在学", "unlocked": "已会", "mastered": "很熟练"}
DOMAIN_CN = {"language": "语言", "social": "社交", "life": "生活技能", "emotion": "情绪", "cognition": "认知"}
EMPTY = "她还没有留下可以查看的内容，先和她聊几句吧～"


def _bar(x: float, n: int = 10) -> str:
    k = max(0, min(n, round(x * n)))
    return "█" * k + "░" * (n - k)


def _has(ro: ReadOnlyDB, table: str) -> bool:
    return bool(ro.query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def render_status(ro: ReadOnlyDB, clock: Clock, cfg: dict[str, Any]) -> str:
    if not _has(ro, "growth_state") or not ro.query_one("SELECT 1 FROM growth_state"):
        return EMPTY
    now = clock.now()
    traits, style = PersonaStore.read_traits(ro), PersonaStore.read_style(ro)
    gs = GrowthEngine.read_state(ro)
    rel = RelationshipSystem(None).father(ro)  # type: ignore[arg-type]
    attach = rel.attachment if rel else 0.5
    emo = EmotionEngine(None, float(cfg.get("emotion_decay_speed", 1.0)))  # type: ignore[arg-type]
    est = emo.project(emo.load(ro), now)                       # 纯计算：推算到“现在”，不写库
    snap = EmotionEngine.snapshot(est, traits, attach)
    body = BodyModel(None, clock).step(BodyModel.read(ro), now, gs["stage"])  # type: ignore[arg-type]
    att = AttentionModel.read(ro)
    ts = ro.query_one("SELECT * FROM time_sense WHERE id=1") or {}
    tsense = clock.sense(ts.get("last_contact_at"), now)
    v_days = (now - gs["stage_since"]) / 86400 * float(cfg.get("growth_speed", 1.0))
    lines = [f"【{style.get('name', '她')}现在的状态】",
             f"成长阶段：{STAGE_CN.get(gs['stage'], gs['stage'])}（本阶段已过约 {v_days:.1f} 个成长日，认知水平 {gs['cognition_level']:.2f}）",
             "", "情绪（已推算到现在）："]
    for e in EMOTIONS:
        lines.append(f"  {EMOTION_CN[e]:<2} {_bar(est.values[e])} {est.values[e]:.2f}")
    extra = []
    if snap.mixed:
        extra.append("心情有点矛盾")
    if snap.tsundere:
        extra.append("嘴硬心软")
    if snap.regret > 0.1:
        extra.append(f"有点后悔(≈{snap.regret:.2f})")
    lines.append(f"  主导情绪：{EMOTION_CN[snap.dominant]}" + (f"；{'、'.join(extra)}" if extra else ""))
    lines += ["", f"身体感：{PHASE_CN.get(body.phase, body.phase)}｜精力 {body.energy:.2f}｜困倦 {body.sleepiness:.2f}",
              f"时间感：{tsense.weekday_cn}{tsense.period}｜{tsense.absence_label}"
              + (f"（距上次对话 {human_ago(tsense.since_last_contact_s)}）" if tsense.since_last_contact_s else ""),
              f"注意力：专注 {att.focus:.2f}"
              + (f"｜{att.away_reason}（到 {clock.local(att.away_until):%H:%M}）" if att.away_until > now else "")
              + f"｜社交需求 {att.social_need:.2f}｜想独处 {att.solitude_need:.2f}"]
    u = ro.query_one("SELECT * FROM token_usage ORDER BY id DESC LIMIT 1") if _has(ro, "token_usage") else None
    if u:
        lines.append(f"\n最近一次上下文保守估算：{u['total']}/{u['budget']} token"
                     f"（固定{u['fixed']}·状态{u['state']}·检索{u['retrieval']}·对话{u['dialog']}）")
    return "\n".join(lines)


def render_memory_list(ro: ReadOnlyDB, clock: Clock, cfg: dict[str, Any], page: int = 1, page_size: int = 12) -> str:
    if not _has(ro, "memories"):
        return EMPTY
    now, speed = clock.now(), float(cfg.get("memory_decay_speed", 1.0))
    rows = [row_to_memory(r) for r in ro.query("SELECT * FROM memories ORDER BY id DESC LIMIT 800")]
    if not rows:
        return "她还没有形成任何记忆～"
    ranked = sorted(((effective_weight(m, now, speed), m) for m in rows), key=lambda x: -x[0])
    pages = max(1, (len(ranked) + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    out = [f"【记忆列表】按当前权重排序，共 {len(ranked)} 条（第 {page}/{pages} 页）；用 /记忆详情 编号 查看"]
    for w, m in ranked[(page - 1) * page_size: page * page_size]:
        emo = EMOTION_CN[dominant_emotion(m["emotion"])] if m["emotion"] and m["emotion_intensity"] > 0.15 else "平静"
        flags = ("〔已压缩〕" if m["compressed"] else "") + ("〔保护〕" if m["protected"] else "")
        txt = (m["gist"] or m["content"]).replace("\n", " ")[:26]
        out.append(f"#{m['id']} [{MEMORY_TYPE_CN.get(m['type'], m['type'])}] {txt}… ｜权重{w:.2f} ｜{emo}{m['emotion_intensity']:.2f}"
                   f" ｜召回{m['recall_count']}次 {flags}")
    return "\n".join(out)


def render_memory_detail(ro: ReadOnlyDB, clock: Clock, cfg: dict[str, Any], n: int) -> str:
    if not _has(ro, "memories"):
        return EMPTY
    r = ro.query_one("SELECT * FROM memories WHERE id=?", (n,))
    if not r:
        return f"没有找到编号 #{n} 的记忆（用 /记忆列表 看看有哪些）～"
    m = row_to_memory(r)
    now, speed = clock.now(), float(cfg.get("memory_decay_speed", 1.0))
    w = effective_weight(m, now, speed)
    out = [f"【记忆 #{m['id']}】类型：{MEMORY_TYPE_CN.get(m['type'], m['type'])}｜创建于 {human_ago(now - m['created_at'])}",
           f"范围：{m.get('scope_id', 'legacy:father')}｜来源：{m.get('source_kind', 'experience')}｜置信度：{m.get('confidence', 0.7):.2f}",
           f"原文：{m['content']}",
           f"她现在的理解：{m['gist']}" + (f"（被重构过 {m['reconstruct_count']} 次）" if m["reconstruct_count"] else ""),
           f"权重：当前 {w:.2f}（库存 {m['weight']:.2f}｜重要度 {m['importance']:.2f}｜衰减率 {m['decay_rate']:.2f}）",
           f"清晰度：{m['clarity']:.2f}" + ("（她会说“记不太清了”）" if m["clarity"] < 0.4 and not m["protected"] else ""),
           "情绪坐标：效价 {:.2f}｜唤醒度 {:.2f}｜强度 {:.2f}｜".format(m["valence"], m["arousal"], m["emotion_intensity"])
           + " ".join(f"{EMOTION_CN[e]}{m['emotion'].get(e, 0):.2f}" for e in EMOTIONS if m["emotion"].get(e, 0) >= 0.15),
           f"召回次数：{m['recall_count']}｜最后回忆：{human_ago(now - m['last_recalled_at']) if m['last_recalled_at'] else '从未被主动想起'}",
           f"巩固状态：{m['consolidation']}｜沉睡：{'是' if m['dormant'] else '否'}｜保护(不可压缩)：{'是' if m['protected'] else '否'}"
           f"｜未解决：{'是' if m['unresolved'] else '否'}"]
    if m["compressed"]:
        out.append(f"已压缩：是（注入时用语义认知 #{m['compressed_into']} 代替；原始记忆仍完整保存）")
    else:
        out.append("已压缩：否")
    if m["source_ids"]:
        out.append(f"原始记忆引用：{', '.join('#' + str(i) for i in m['source_ids'][:12])}")
    links = ro.query("SELECT * FROM memory_links WHERE src=? OR dst=? ORDER BY strength DESC LIMIT 8", (n, n))
    if links:
        out.append("关联：" + "；".join(f"#{l['dst'] if l['src'] == n else l['src']}({l['kind']},{l['strength']:.2f})" for l in links))
    revs = ro.query("SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY id DESC LIMIT 3", (n,))
    for rv in revs:
        out.append(f"理解的变化：{human_ago(now - rv['created_at'])}｜“{(rv['old_gist'] or '')[:20]}”→“{rv['new_gist'][:30]}”（{rv['reason']}）")
    return "\n".join(out)


def render_growth_report(ro: ReadOnlyDB, clock: Clock, cfg: dict[str, Any]) -> str:
    if not _has(ro, "growth_state") or not ro.query_one("SELECT 1 FROM growth_state"):
        return EMPTY
    gs = GrowthEngine.read_state(ro)
    caps = GrowthEngine.read_caps(ro)
    now = clock.now()
    out = [f"【成长报告】阶段：{STAGE_CN.get(gs['stage'], gs['stage'])}｜认知水平 {gs['cognition_level']:.2f}｜累计经历 {gs['total_experience']} 次"]
    for dom in ("language", "social", "life", "emotion", "cognition"):
        out.append(f"\n▍{DOMAIN_CN[dom]}")
        for d in [c for c in CAP_POOL if c.domain == dom]:
            c = caps.get(d.key)
            if not c:
                continue
            out.append(f"  {d.title:<8} {_bar(c['proficiency'], 8)} {c['proficiency']:.2f}｜{STATE_CN.get(c['unlock_state'], c['unlock_state'])}"
                       f"｜练习{c['practice_count']}/{d.req_practice}｜经历{c['experience_count']}/{d.req_experience}"
                       + (f"｜退步{c['regress_count']}次" if c["regress_count"] else ""))
    ms = ro.query("SELECT * FROM milestones ORDER BY created_at") if _has(ro, "milestones") else []
    out.append("\n▍成长里程碑")
    out += [f"  {clock.local(m['created_at']):%m-%d %H:%M}  {m['title']}（{m['description'][:24]}）" for m in ms] or ["  还没有，慢慢来～"]
    return "\n".join(out)


def render_relationship(ro: ReadOnlyDB, clock: Clock, cfg: dict[str, Any]) -> str:
    if not _has(ro, "relationships"):
        return EMPTY
    rs = RelationshipSystem(None)  # type: ignore[arg-type]
    rel = rs.father(ro)
    if not rel:
        rows = ro.query('SELECT actor_id,role,trust,interactions FROM social_relationships ORDER BY last_seen_at DESC LIMIT 12')
        return '【社交关系】\n' + ('\n'.join(f"{r['actor_id']}：{r['role']}，信任{r['trust']:.2f}，交流{r['interactions']}次" for r in rows)
                                  or '暂未建立关系。')
    now = clock.now()
    traits = PersonaStore.read_traits(ro)
    RelationshipSystem.update_longing(rel, now, traits.get("clinginess", 0.5))   # 纯计算（rel 是副本，不写库）
    out = [f"【关系状态：爸爸】",
           f"信任 {_bar(rel.trust)} {rel.trust:.2f}",
           f"依恋 {_bar(rel.attachment)} {rel.attachment:.2f}",
           f"安全感 {_bar(rel.security)} {rel.security:.2f}｜想念 {rel.longing:.2f}",
           f"共同记忆：{rel.shared_memory_count} 条｜冲突未解决：{'是' if rel.conflict_open else '否'}",
           f"最近的关系变化：{rel.last_change_note or '暂无'}"]
    evs = rs.recent_events(rel.person_id, 5, ro)
    if evs:
        out.append("最近事件：" + "；".join(f"{human_ago(now - e['created_at'])}{e['kind']}(信任{e['d_trust']:+.3f}/依恋{e['d_attachment']:+.3f})" for e in evs))
    pr = (ro.query_one("SELECT COUNT(*) n FROM promises WHERE status='open'") or {"n": 0})["n"]
    ws = (ro.query_one("SELECT COUNT(*) n FROM wishes WHERE status='open'") or {"n": 0})["n"]
    out.append(f"她记着的约定：{pr} 个｜她的小愿望：{ws} 个")
    for r in ro.query('SELECT actor_id,role,trust,interactions FROM social_relationships ORDER BY last_seen_at DESC LIMIT 12'):
        out.append(f"{r['actor_id']}：{r['role']}，信任{r['trust']:.2f}，交流{r['interactions']}次")
    for r in ro.query("SELECT scope_id,next_allowed_at,frustration FROM speech_cooldowns WHERE scope_id!='__global_speech__' ORDER BY updated_at DESC LIMIT 5"):
        out.append(f"会话{r['scope_id']}：受挫{r['frustration']:.2f}，冷却剩余{max(0,int(r['next_allowed_at']-now))}秒")
    return "\n".join(out)


def render_last_reflection(ro: ReadOnlyDB, clock: Clock, cfg: dict[str, Any]) -> str:
    if not _has(ro, "reflections"):
        return EMPTY
    r = ro.query_one('SELECT * FROM reflections ORDER BY id DESC LIMIT 1')
    if not r:
        return "她最近还没有认真复盘过什么（她想想的时候才会有，不是定时的）～"
    now = clock.now()
    depth = {1: "随手想想", 2: "认真想想", 3: "深度复盘"}.get(r["depth"], str(r["depth"]))
    trig = "、".join(jload(r["trigger_json"], []) or []) or "说不上来，就是想想"
    comp = ro.kv_get("last_reflection_compressed", 0) if _has(ro, "kv") else 0
    return "\n".join([
        f"【最近一次复盘】{human_ago(now - r['created_at'])}｜{depth}（冲动 {r['pull']:.2f}）",
        f"为什么会想：{trig}",
        f"她的总结：{r['summary']}",
        f"她学到了：{r['learned'] or '（没特别总结）'}",
        f"这次压缩了 {comp} 簇琐碎记忆｜写日记：{'是' if r['diary'] else '否'}"
        f"｜想告诉爸爸：{r['share_intent']:.2f}（{'已说过' if r['shared_with_father'] else '还没说'}）",
    ])
