"""CompanionEngine：把各模块串成对话主链路（不依赖 AstrBot，便于测试）。

对话流程：接收 → 记忆/情绪 → 意图判断 → 认知 → 表达规划与节制 → 分层上下文 →
生成与人格校验 → 形态控制 → 平台发送 → 确认实际内容 → 练习/记忆 → 自主复盘。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
from dataclasses import replace
from typing import Any, Callable

from .budget import ContextBudgetManager, UsageMonitor
from .behavior_state import BehaviorStore
from .consistency import PersonaConsistency
from .expression import shape_reply
from .clock import Clock
from .cognition import CognitionEngine
from .db import Database, ReadOnlyDB
from .emotion import EmotionEngine
from .growth import GrowthEngine, GrowthEvent
from .inner_life import InnerLife
from .life_tick import LifeTick
from .llm import LLMAdapter
from .memory import MemorySystem, similarity
from .models import (EMOTION_CN, Appraisal, AssembledContext, EmotionSnapshot, EngineReply, STAGE_CN, BehaviorPlan, Percept, clamp)
from .perception import bigrams, perceive
from .persona import AttentionModel, BodyModel, PersonaStore
from .planner import BehaviorPlanner, PlanContext
from .prompts import (build_fixed_layer, build_fixed_short, build_retrieval_segments, build_state_segments,
                      fixed_cache_key)
from .reflection import ReflectionEngine
from .relationship import RelationshipSystem
from .social import SocialSystem

log = logging.getLogger("growing_companion")

DEFAULTS: dict[str, Any] = {
    "growth_speed": 1.0, "emotion_decay_speed": 1.0, "memory_decay_speed": 1.0,
    "token_budget_total": 4000, "token_budget_fixed": 1200, "token_budget_state": 900,
    "token_budget_retrieval": 1200, "token_budget_dialog": 1600,
    "memory_topk": 6, "dialog_window_turns": 12, "timezone_offset_hours": 8.0,
    "father_ids": [], "tick_interval_seconds": 480, "human_like_delay": True, "max_reply_delay_seconds": 8,
    "social_cooldown_seconds": 90, "social_global_cooldown_seconds": 10,
    "social_ack_timeout_seconds": 180, "social_hourly_limit": 6, "social_global_hourly_limit": 24,
    "llm_timeout_seconds": 45,
    "persona_init": {"name": "小满", "introversion": 0.5, "sensitivity": 0.6, "patience": 0.5,
                     "clinginess": 0.6, "humor": 0.5, "stubbornness": 0.5, "openness": 0.6,
                     "initial_stage": "infant"},
}

_BREAK_RE = re.compile(r"我(只)?是(一个|个)?(AI|人工智能|程序|语言模型|机器人)|作为(一个)?(AI|人工智能|语言模型)", re.I)
_SENT_RE = re.compile(r"(?<=[。！？!?…~\n])")


def make_tags(period: str, dominant: str, keywords: list[str]) -> list[str]:
    """记忆的情境标签：时段/情绪/人物/关键词——关联触发靠这些多维线索，而不只是关键词。"""
    return [f"p:{period}", f"e:{dominant}", "who:father"] + [f"k:{k}" for k in keywords[:4]]


def enforce_ceiling(text: str, max_chars: int) -> str:
    """语言能力上限的硬护栏：她“还说不了”那么长的话时，在句读处截断。"""
    limit = int(max_chars * 1.25) + 2
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]                      # 给省略号留一个位置，保证最终长度 ≤ limit
    idx = max(cut.rfind(c) for c in "。！？!?…~，,、 ")
    if idx >= max(2, int(limit * 0.4)):
        cut = cut[:idx + 1]
    cut = cut.rstrip("，, 、")
    return cut if cut[-1:] in "。！？!?…~" else cut + "…"


class CompanionEngine:
    def __init__(self, db_path: str, config: dict[str, Any] | None = None, *, llm: LLMAdapter | None = None,
                 clock: Clock | None = None, rng: random.Random | None = None):
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in (config or {}).items() if v is not None})
        pi = dict(DEFAULTS["persona_init"])
        pi.update(cfg.get("persona_init") or {})
        cfg["persona_init"] = pi
        cfg['token_budget_total'] = max(3000, min(5000, int(cfg['token_budget_total'])))
        cfg['max_reply_delay_seconds'] = max(1, min(30, float(cfg['max_reply_delay_seconds'])))
        for key, low, high in [('social_cooldown_seconds', 10, 86400),
                               ('social_global_cooldown_seconds', 3, 86400),
                               ('social_ack_timeout_seconds', 10, 86400),
                               ('social_hourly_limit', 1, 60), ('social_global_hourly_limit', 1, 240),
                               ('llm_timeout_seconds', 5, 120)]:
            cfg[key] = max(low, min(high, int(cfg[key])))
        self.cfg = cfg
        self.rng = rng or random.Random()
        self.clock = clock or Clock(float(cfg["timezone_offset_hours"]))
        self.db = Database(db_path)
        self.db_path = db_path
        self.llm = llm
        self.persona = PersonaStore(self.db)
        self.emotion = EmotionEngine(self.db, float(cfg["emotion_decay_speed"]))
        self.body_model = BodyModel(self.db, self.clock)
        self.attention_model = AttentionModel(self.db)
        self.relations = RelationshipSystem(self.db)
        self.memory = MemorySystem(self.db, self.clock, float(cfg["memory_decay_speed"]), self.rng)
        self.growth = GrowthEngine(self.db, self.clock, float(cfg["growth_speed"]), self.rng)
        self.cognition = CognitionEngine(self.db)
        self.inner = InnerLife(self.db)
        self.reflection = ReflectionEngine(self.db, self.memory, self.growth, self.cognition, self.inner, self.rng)
        self.planner = BehaviorPlanner()
        self.behavior = BehaviorStore(self.db)
        self.consistency = PersonaConsistency()
        self._pending: dict[str, Callable[[list[str]], None]] = {}
        self.budget = ContextBudgetManager(
            total=int(cfg["token_budget_total"]), fixed_max=int(cfg["token_budget_fixed"]),
            state_max=max(800, int(cfg["token_budget_state"])), retrieval_max=int(cfg["token_budget_retrieval"]),
            dialog_max=int(cfg["token_budget_dialog"]), window_turns=int(cfg["dialog_window_turns"]),
            topk=int(cfg["memory_topk"]))
        self.monitor = UsageMonitor(self.db)
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._reflection_task: asyncio.Task | None = None
        self._seed(self.clock.now())
        self.social = SocialSystem(self)

    # ------------------------------------------------------------------ 初始化
    def _seed(self, now: float) -> None:
        pi = self.cfg["persona_init"]
        self.persona.ensure_seed(now, pi)
        self.emotion.ensure_seed(now, self.persona.traits())
        self.body_model.ensure_seed(now)
        self.attention_model.ensure_seed(now)
        self.growth.ensure_seed(now, str(pi.get("initial_stage", "infant")))
        self.cognition.ensure_seed(now)
        self.db.execute("INSERT OR IGNORE INTO time_sense(id,born_at,first_chat_at,last_contact_at,last_tick_at,"
                        "last_gap_s,birthday_md) VALUES(1,?,NULL,NULL,?,NULL,?)",
                        (now, now, self.clock.local(now).strftime("%m-%d")))

    # ------------------------------------------------------------------ 身份
    def resolve_father(self, user_id: str, now: float, bind: bool = True) -> bool | None:
        """身份只能由配置授予；群聊或陌生人的自我声明不产生父亲权限。"""
        uid = str(user_id)
        ids = [str(x) for x in (self.cfg.get("father_ids") or [])]
        if ids:
            ok = uid in ids
        else:
            ok = False
        if ok and bind:
            self.relations.ensure(uid, "father", now)
        return ok

    def is_father(self, user_id: str) -> bool:
        return bool(self.resolve_father(user_id, self.clock.now(), bind=False))

    def open_readonly(self) -> ReadOnlyDB:
        return ReadOnlyDB(self.db_path)

    # ------------------------------------------------------------------ 主链路
    async def handle_message(self, user_id: str, text: str, *, now: float | None = None, is_voice: bool = False,
                             llm: LLMAdapter | None = None, spawn_reflection: bool = True,
                             scope_id: str = '', is_group: bool = False, directed: bool = True,
                             message_id: str = '') -> EngineReply:
        async with self._lock:
            if is_group or not self.is_father(user_id):
                return await self.social.handle(user_id, text, scope_id or f'private:{user_id}',
                    is_group=is_group, directed=directed, message_id=message_id, llm=llm or self.llm,
                    now=self.clock.now() if now is None else now, spawn_reflection=spawn_reflection)
            return await self._handle(user_id, text, now, is_voice, llm or self.llm, spawn_reflection)

    async def _handle(self, user_id: str, text: str, now: float | None, is_voice: bool,
                      llm: LLMAdapter | None, spawn_reflection: bool) -> EngineReply:
        now = self.clock.now() if now is None else now
        is_father = bool(self.resolve_father(user_id, now))
        LifeTick.run(self, now)                                  # 追上流逝的时间

        # ---- 感知 ----
        ts = self.db.query_one("SELECT * FROM time_sense WHERE id=1") or {}
        gap = (now - ts["last_contact_at"]) if ts.get("last_contact_at") else None
        percept = perceive(text, user_id, now, gap, is_voice)
        traits, style = self.persona.traits(), self.persona.style()
        tsense = self.clock.sense(ts.get("last_contact_at"), now)
        stage = self.growth.state()["stage"]
        rel = self.relations.get(str(user_id)) if is_father else None
        attach = rel.attachment if rel else 0.2

        # ---- 身体与注意力（被叫醒/被关注）----
        body = self.body_model.step(self.body_model.load(), now, stage)
        body = self.body_model.wake_by_message(body, now)
        att, was_away = AttentionModel.on_message(self.attention_model.load(), now, traits)

        # ---- 记忆：主动浮现 + 关联检索（top-k）----
        est = self.emotion.project(self.emotion.load(), now)
        snap0 = EmotionEngine.snapshot(est, traits, attach)
        tags_now = make_tags(tsense.period, snap0.dominant, percept.keywords)
        recalled, spont = [], None
        if is_father:
            recalled = self.memory.retrieve(percept.text, percept.keywords, now, self.budget.topk, snap0, tags_now)
            spont = self.memory.surface(now, snap0, self.rng, body.energy)
        reunion = False
        if rel:
            rel, reunion = self.relations.on_contact(rel, now)

        # ---- 情绪更新（含想起往事的情绪后效）----
        deltas = self.emotion.appraise(percept, longing=rel.longing if rel else 0.0, attachment=attach)
        for e, dv in self.emotion.mood_congruent_delta([r.memory for r in recalled]).items():
            deltas[e] += dv
        est = self.emotion.apply(est, deltas, traits["sensitivity"], attach, now)

        # ---- 先产生自己的意图，再理解输入并校正表达方向 ----
        snap_mid = EmotionEngine.snapshot(est, traits, attach)
        profile = self.growth.profile(body, snap_mid)
        history = self.behavior.history('legacy:father', str(user_id), now)
        preferences = self.db.query('SELECT subject,strength FROM preferences WHERE valence>0 ORDER BY strength DESC LIMIT 10')
        ctx = PlanContext(
            percept=percept, appraisal=Appraisal(), snap=snap_mid, body=body, attention=att, was_away=was_away,
            relation=rel, traits=traits, style=style, profile=profile, time=tsense, recalled=recalled,
            spontaneous=spont, reunion=reunion, pending_reflection=self.reflection.pending_share(),
            pending_dream=self.inner.unshared_dream(), open_promises=self.inner.open_promises(),
            is_father=is_father, history=history,
            interest=max((r['strength'] for r in preferences if r['subject'] in text), default=0.0))
        intent = self.planner.infer_intent(ctx, self.rng)
        ap = self.cognition.appraise(percept, recalled, profile, snap_mid, self.rng)
        if ap.novelty > 0:       # 新鲜事勾起好奇
            est = self.emotion.apply(est, {"curiosity": 0.10 * ap.novelty}, traits["sensitivity"], attach, now)
        if ap.value_conflict or ap.misread or ap.unknown_topic:
            self.db.kv_set("cognitive_conflicts", float(self.db.kv_get("cognitive_conflicts", 0) or 0) + 1)
        snap = EmotionEngine.snapshot(est, traits, attach)
        self.emotion.save(est)

        # ---- 关系变化与成长经历 ----
        flags: list[str] = []
        if rel:
            for kind in ("praise", "comfort", "scold", "apology"):
                if kind in percept.cues:
                    flags += self.relations.apply_event(rel, kind, now)
            self.relations.apply_event(rel, "chat", now)
            self.growth.observe(percept, now)
        profile = self.growth.profile(body, snap)

        # ---- 行为决策 ----
        ctx.appraisal, ctx.snap, ctx.profile = ap, snap, profile
        plan = self.planner.plan_expression(ctx, intent, self.rng)

        # ---- 上下文预算管理器：分层装配 ----
        dialog = self._recent_dialog(str(user_id))
        beliefs = self.cognition.top_beliefs(percept.text, 2) if is_father else []
        last_ref = self.reflection.last_reflection(self.db) if is_father else None
        if last_ref and similarity(bigrams(percept.text), bigrams(last_ref["summary"])) < 0.1:
            last_ref = None
        ms_text = self._relevant_milestone(percept.text) if is_father else None
        r_segs = build_retrieval_segments(recalled + ([spont] if spont else []), beliefs, last_ref, ms_text)
        guidance = self.social.guidance(text, now) if is_father else None
        state_segs = build_state_segments(ctx, plan)
        if guidance and plan.intent in {'share', 'question'} and plan.language_tier >= 2 and not plan.safety:
            state_segs.append((f'今天在群里想到“{guidance["topic"]}”，想问爸爸怎么看；不转述群友身份或私事。', 12))
        def assemble(extra: list[tuple[str, float]]) -> AssembledContext:
            return self.budget.assemble(
            fixed_key=fixed_cache_key(style, traits, stage, plan.language_tier),
            fixed_full=lambda: build_fixed_layer(style, traits, stage, plan.language_tier),
            fixed_short=lambda: build_fixed_short(style, traits, stage, plan.language_tier),
            state_segs=extra + state_segs, retrieval_segs=r_segs, dialog=dialog,
            user_text=percept.text)
        reply, asm = await self.generate(assemble, plan, ap, snap, llm, history, now)
        parts = shape_reply(reply, plan, max_delay=float(self.cfg['max_reply_delay_seconds']),
                            delay_enabled=bool(self.cfg['human_like_delay']))
        reply = '\n'.join(parts)
        behavior_id = self.behavior.record('legacy:father', str(user_id), plan, now,
                          history['fatigue_load'] + max(0, .65 - body.energy))

        # ---- 回复之后：成长、状态、记忆、复盘 ----
        BodyModel.spend(body, snap.arousal, len(percept.text))
        self.body_model.save(body)
        self.attention_model.save(att)
        self.db.execute("INSERT INTO conversation_log(ts,user_id,role,content) VALUES(?,?,?,?)",
                        (now, str(user_id), "user", percept.text))
        self.db.execute("UPDATE time_sense SET last_contact_at=?, first_chat_at=COALESCE(first_chat_at,?), "
                        "last_gap_s=? WHERE id=1", (now, now, gap))
        if rel:
            self.relations.save(rel)
        token = secrets.token_hex(16)

        def after_delivery(delivered: list[str]) -> None:
            sent = '\n'.join(delivered)
            delivered_at = self.clock.now()
            self.behavior.complete(behavior_id, delivered, delivered_at, expected=len(parts))
            if sent:
                events = self.growth.after_reply(sent, percept, body, snap, delivered_at)
                self.handle_growth_events(events, delivered_at)
                self.db.execute("INSERT INTO conversation_log(ts,user_id,role,content) VALUES(?,?,?,?)",
                                (delivered_at, str(user_id), 'assistant', sent))
                self.social.mark_guidance_offered(guidance, sent, delivered_at)
            # 观察仍形成经历；未说出口的文本不进入记忆或练习次数。
            actual_plan = replace(plan, share_reflection_id=None, share_dream_id=None)
            if sent and ctx.pending_reflection and similarity(bigrams(sent), bigrams(ctx.pending_reflection['summary'])) > .15:
                actual_plan.share_reflection_id = plan.share_reflection_id
            if sent and ctx.pending_dream and similarity(bigrams(sent), bigrams(ctx.pending_dream['content'])) > .15:
                actual_plan.share_dream_id = plan.share_dream_id
            self._after_reply_records(percept, sent or '（没有发出回复）', snap, ap, actual_plan, rel,
                                      flags, recalled, spont, tsense.period, now, is_father)
            if rel:
                self.relations.save(rel)
            if spawn_reflection:
                self._spawn_reflection(delivered_at, body, snap, attach, llm, style, traits.get('openness', .5))

        self._pending[token] = after_delivery
        return EngineReply(text=reply, plan=plan, usage=asm.usage, silent=plan.silent,
                           delay_s=plan.delay_seconds, prefer_voice=plan.prefer_voice,
                           speech_token=token, scope_id='legacy:father', parts=parts, behavior_id=behavior_id)

    async def complete_reply(self, reply: EngineReply, delivered: list[str]) -> None:
        """发送方必须在 finally 调用；成功、失败、取消、沉默都可重复确认而不重复练习。"""
        if reply.speech_token in self.social.pending:
            await self.social.complete(reply, bool(delivered), delivered_parts=delivered)
            return
        async with self._lock:
            callback = self._pending.pop(reply.speech_token, None)
            if callback:
                callback(delivered)

    async def generate(self, assemble: Callable[[list[tuple[str, float]]], AssembledContext],
                       plan: BehaviorPlan, ap: Appraisal, snap: EmotionSnapshot, llm: LLMAdapter | None,
                       history: dict[str, Any], now: float, *, require_output: bool = False) -> tuple[str, AssembledContext]:
        """至多一次重生成，两次都走同一个分层预算；最终还有规则节制层。"""
        asm = assemble([])
        self.monitor.record(asm.usage, now)
        if plan.silent:
            return '', asm
        raw = ''
        score = 0.0
        for attempt in range(2):
            if llm:
                try:
                    raw = await asyncio.wait_for(llm.chat(prompt=asm.prompt, system_prompt=asm.system_prompt,
                        contexts=asm.contexts, purpose='reply' if attempt == 0 else 'persona_repair'),
                        self.cfg['llm_timeout_seconds'])
                except Exception as exc:
                    log.warning('LLM 调用失败：%s', type(exc).__name__)
                    if require_output and not raw:
                        raise RuntimeError('社交模型不可用') from exc
            if require_output and not raw:
                raise RuntimeError('模型没有返回文字')
            check = self.consistency.check(raw, plan, history)
            score = max(score, check.score)
            if check.score < .4 or attempt or not llm or ap.sincere_identity_question:
                break
            # 不重放漂移候选。只传校验原因，重新回应原始输入，避免复制错误句式。
            asm = assemble([('重写要求：' + '、'.join(check.reasons) + '。用日常口语回应，不汇报内部状态。', 31)])
            self.monitor.record(asm.usage, now)
        plan.persona_drift_score, plan.last_consistency_check = score, now
        if not ap.sincere_identity_question:
            raw = self.consistency.clean(raw, plan, history)
            if raw in history.get('recent_texts', [])[:1] and len(raw) > 8:
                raw = ''
        return self._postprocess(raw, plan, ap.sincere_identity_question, snap), asm

    # ------------------------------------------------------------------ 回复后处理
    def _postprocess(self, raw: str, plan: BehaviorPlan, identity_q: bool, snap: EmotionSnapshot) -> str:
        text = (raw or "").strip()
        if identity_q:
            return '我是陪伴你的 AI，没有真实的生物身体；平时我们可以按女儿的角色相处。'
        if text and not identity_q:    # 没被认真问到时，不主动破坏沉浸感
            parts = [s for s in _SENT_RE.split(text) if s and not _BREAK_RE.search(s)]
            text = "".join(parts).strip()
        if not text:
            text = self._fallback_text(plan, snap)
        return enforce_ceiling(text, plan.max_chars)

    def _fallback_text(self, plan: BehaviorPlan, snap: EmotionSnapshot) -> str:
        r = self.rng
        if plan.safety:
            return '我在听。你现在安全吗？请先找身边信任的人陪你，必要时联系医生或当地紧急帮助。'
        if plan.mode == "sulk":
            return r.choice(["哼。", "不理你…", "哼～"]) if plan.language_tier >= 1 else "哼。"
        if plan.mode == "sleepy":
            return r.choice(["嗯…", "唔…", "嗯。"])
        tiers = {0: ["啊…", "爸…", "嗯嗯", "呀～"], 1: ["爸爸！", "要抱抱", "嗯嗯好", "玩！"],
                 2: ["嗯。", "我在呢", "好呀", "唔…"]}
        return r.choice(tiers.get(plan.language_tier, ["嗯，我在听呢，爸爸。", "我在呀。"]))

    # ------------------------------------------------------------------ 记录
    def _recent_dialog(self, user_id: str) -> list[dict[str, str]]:
        rows = self.db.query("SELECT role, content FROM conversation_log WHERE user_id=? AND scope_id='legacy:father' ORDER BY id DESC LIMIT ?",
                             (user_id, int(self.cfg["dialog_window_turns"]) * 2))
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def _relevant_milestone(self, text: str) -> str | None:
        qbg = bigrams(text)
        for r in self.db.query("SELECT title, description FROM milestones ORDER BY id DESC LIMIT 5"):
            if similarity(qbg, bigrams(r["title"] + r["description"])) > 0.1:
                return f"{r['title']}。{r['description'][:40]}"
        return None

    def _after_reply_records(self, p: Percept, reply: str, snap: EmotionSnapshot, ap: Any, plan: BehaviorPlan,
                             rel: Any, flags: list[str], recalled: list[Any], spont: Any, period: str,
                             now: float, is_father: bool) -> list[str]:
        out: list[str] = []
        if not is_father:
            return out
        mid: int | None = None
        cues_rel = p.cues & {"praise", "scold", "comfort", "apology", "promise", "call_dad"}
        importance = 0.12 + 0.4 * snap.intensity + 0.10 * ap.novelty + (0.25 if cues_rel else 0.0)
        if len(p.text) < 4 and not p.cues:
            importance -= 0.1
        unresolved = "scold" in p.cues
        if importance >= 0.2 or unresolved:      # 琐碎的话不进长期记忆，只留在对话日志里，自然淡出
            mtype = "relational" if cues_rel else "episodic"
            tags = make_tags(period, snap.dominant, p.keywords)
            mid = self.memory.add(mtype=mtype, content=f"爸爸：{p.text}\n我：{reply}", now=now, snap=snap,
                                  importance=clamp(importance), tags=tags, unresolved=unresolved,
                                  protected=True if ("promise" in p.cues or unresolved) else None)
            if snap.intensity >= 0.5:
                eid = self.memory.add(mtype="emotional", link=False, now=now, snap=snap,
                                      content=f"{EMOTION_CN[snap.dominant]}的感觉：{p.text[:30]}",
                                      importance=clamp(importance), tags=tags)
                self.memory.link(eid, mid, "emotional", 0.6)
            if rel:
                rel.shared_memory_count += 1
            out.append(f"memory#{mid}")
        if "apology" in p.cues:                  # 道歉/和好：冲突“放下”了（记忆仍保留原样）
            self.db.execute("UPDATE memories SET unresolved=0 WHERE unresolved=1")
        self.memory.reinforce(recalled + ([spont] if spont else []), now)
        self.inner.update_preferences(p.text, snap, now)
        self.inner.note_wishes_and_promises(p, mid, now)
        for ph in self.persona.learn_phrases(p.text, now):
            out.append(f"学会口癖:{ph}")
        if plan.share_reflection_id:
            self.reflection.mark_shared(plan.share_reflection_id)
        if plan.share_dream_id:
            self.inner.mark_dream_shared(plan.share_dream_id)
        stage = self.growth.state()["stage"]
        if "first_conflict" in flags:
            self._milestone("conflict", "第一次吵架", "爸爸凶了我，我又难过又生气", now, stage, protected_type="relational")
        if "first_reconcile" in flags:
            self._milestone("reconcile", "第一次和好", "爸爸跟我道歉了，我们和好了", now, stage, protected_type="relational")
        return out

    # ------------------------------------------------------------------ 里程碑
    def _milestone(self, kind: str, title: str, desc: str, now: float, stage: str,
                   protected_type: str = "episodic") -> bool:
        if self.db.query_one("SELECT 1 FROM milestones WHERE kind=? AND title=?", (kind, title)):
            return False
        joy = EmotionSnapshot(values={"joy": 0.8, "trust": 0.7, "fear": 0.05, "sadness": 0.05,
                                      "curiosity": 0.4, "anger": 0.02}, dominant="joy", intensity=0.8,
                              valence=0.8, arousal=0.6)
        mid = self.memory.add(mtype=protected_type, content=f"【里程碑】{title}。{desc}", now=now, snap=joy,
                              importance=0.9, protected=True, tags=["milestone", f"m:{kind}"])
        self.db.execute("INSERT INTO milestones(kind,title,description,stage,memory_id,created_at) VALUES(?,?,?,?,?,?)",
                        (kind, title, desc, stage, mid, now))
        return True

    def handle_growth_events(self, events: list[GrowthEvent], now: float) -> None:
        """成长事件 → 里程碑 + 重要情景记忆（第一次叫爸爸/第一次完整表达/第一次学会某事/阶段变化）。"""
        for e in events:
            if e.kind == "unlock":
                special = {"call_dad": ("first_call_dad", "第一次叫爸爸", "我第一次清楚地叫出了“爸爸”"),
                           "full_sentences": ("first_full_sentence", "第一次完整表达", "我第一次把一句话完整地说了出来"),
                           "first_words": ("first_words", "第一次蹦出词", "我第一次说出了词，不只是咿呀")}
                kind, title, desc = special.get(e.key, (f"learn_{e.key}", f"第一次学会{e.title}", f"我慢慢学会了{e.title}"))
                stage = e.stage or self.growth.state()["stage"]
                self._milestone(kind, title, desc, now, stage,
                                protected_type="relational" if e.key == "call_dad" else "episodic")
            elif e.kind == "stage":
                self._milestone("stage", f"长大了：{STAGE_CN.get(e.key, e.key)}", "我感觉自己又长大了一点", now, e.key)
            elif e.kind == "regress":
                self.memory.add(mtype="episodic", content=f"最近“{e.title}”好像又生疏了，有点不顺手", now=now,
                                importance=0.25, tags=["growth", "regress"], participants=())
        if events:
            log.debug("成长事件: %s", [(e.kind, e.key) for e in events])

    # ------------------------------------------------------------------ 复盘（异步，不阻塞回复）
    def _spawn_reflection(self, now: float, body: Any, snap: EmotionSnapshot, attach: float,
                          llm: LLMAdapter | None, style: dict[str, Any], openness: float) -> None:
        d = self.reflection.decide(now, body, self.rng)
        if not d.do or (self._reflection_task and not self._reflection_task.done()):
            return

        async def job() -> None:
            try:
                await asyncio.wait_for(self.reflection.run(d, self.clock.now(), llm=llm, style=style,
                    attachment=attach, snap=snap, openness=openness), timeout=self.cfg['llm_timeout_seconds'])
            except Exception:
                log.exception("复盘失败（不影响对话）")

        try:
            t = asyncio.get_running_loop().create_task(job())
        except RuntimeError:
            return
        self._tasks.add(t)
        self._reflection_task = t
        t.add_done_callback(self._tasks.discard)

    async def wait_background(self) -> None:
        """测试/关闭时等待后台复盘结束。"""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ tick / 关闭
    async def tick(self, now: float | None = None) -> dict[str, Any]:
        async with self._lock:
            return LifeTick.run(self, self.clock.now() if now is None else now)

    def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self.db.close()
