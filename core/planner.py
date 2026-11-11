"""4.0：先决定想做什么，再决定怎么表达，最后决定哪些状态值得说出口。"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from .models import (Appraisal, AttentionState, BehaviorPlan, BodyState, EmotionSnapshot, GrowthProfile,
                     IntentDecision, Percept, Recalled, Relationship, SocialDecision, TimeSense, clamp)


@dataclass
class PlanContext:
    percept: Percept
    appraisal: Appraisal
    snap: EmotionSnapshot
    body: BodyState
    attention: AttentionState
    was_away: bool
    relation: Relationship | None
    traits: dict[str, float]
    style: dict[str, Any]
    profile: GrowthProfile
    time: TimeSense
    recalled: list[Recalled] = field(default_factory=list)
    spontaneous: Recalled | None = None
    reunion: bool = False
    pending_reflection: dict[str, Any] | None = None
    pending_dream: dict[str, Any] | None = None
    open_promises: list[dict[str, Any]] = field(default_factory=list)
    is_father: bool = True
    is_group: bool = False
    social_admitted: bool = True
    interest: float = 0.0
    history: dict[str, Any] = field(default_factory=dict)


class BehaviorPlanner:
    @staticmethod
    def social_decision(*, directed: bool, mentioned: bool, role: str, novelty: float,
                        resonance: float, interest: float, share: float, frustration: float,
                        energy: float, curiosity: float, introversion: float,
                        rng: random.Random) -> SocialDecision:
        """软倾向决定想不想冒泡；持久化租约和冷却另行决定能不能发。"""
        if directed:
            return SocialDecision(True, 1.0, '有人直接和我说话')
        drive = max(interest * curiosity, resonance, share * curiosity, 0.8 if mentioned else 0.0)
        if drive < 0.15 or energy < 0.2:
            return SocialDecision(False, 0.0, '继续听大家说话')
        p = clamp((0.04 + 0.4 * drive + 0.08 * novelty) * energy
                  * (1.1 - 0.6 * introversion) * (1 - 0.85 * frustration), 0, 0.55)
        p *= 1.0 if role == 'father' else 0.85 if role == 'familiar' else 0.65
        return SocialDecision(rng.random() < p, p, '兴趣或共鸣；受冷落会退缩')

    def infer_intent(self, ctx: PlanContext, rng: random.Random) -> IntentDecision:
        """第一步：发生在认知判断之前。依赖当下感受、感知与既有关系，不生成台词。"""
        if ctx.is_group and not ctx.social_admitted:
            return IntentDecision('observe', 1, '暂时只听，不参与')
        v, b, a = ctx.snap.values, ctx.body, ctx.attention
        desire = clamp(.52 * b.energy + .22 * a.focus + .15 * a.social_need + .25 * ctx.interest
                       - .42 * a.solitude_need - .22 * b.sleepiness - .25 * v.get('anger', 0))
        if desire < .16:
            intent = 'avoid' if 'question' in ctx.percept.cues or rng.random() < .3 else 'silence'
            return IntentDecision(intent, clamp(.7 + (.16 - desire)), '此刻没有展开谈话的力气')
        if desire < .32 and rng.random() < .7:
            return IntentDecision('avoid', .72, '愿意应一声，但不想延伸')
        if v.get('joy', 0) > .72 and ctx.snap.arousal > .5 and b.energy > .45:
            return IntentDecision('share', .85, '兴奋，有好几件话想说')
        rel = ctx.relation
        if ctx.is_father and (ctx.reunion or (rel and rel.longing > .55)) and rng.random() < .7:
            return IntentDecision('cling', .8, '想靠近熟悉的人')
        if ctx.interest > .45 and v.get('curiosity', 0) > .55:
            pressure = ctx.history.get('question_pressure', 0)
            if rng.random() < ctx.interest * v['curiosity'] * math.exp(-pressure):
                return IntentDecision('question', .7, '这个细节确实勾起兴趣')
        if ctx.is_father and ctx.pending_reflection and rng.random() < ctx.pending_reflection['share_intent'] * desire:
            return IntentDecision('share', .7, '有一件刚想通的事')
        return IntentDecision('respond', clamp(.5 + desire * .4), '接住对方的话')

    def plan_expression(self, ctx: PlanContext, intent: IntentDecision, rng: random.Random) -> BehaviorPlan:
        """第二步：认知判断后规划长度、语气和形态，尚不授权状态自述。"""
        b, a, v, ap = ctx.body, ctx.attention, ctx.snap.values, ctx.appraisal
        budget = clamp(.55 * b.energy + .3 * a.focus + .2 * v.get('joy', 0) - .25 * a.solitude_need)
        pl = BehaviorPlan(intent=intent.intent, intent_confidence=intent.confidence,
                          expression_budget=budget, language_tier=ctx.profile.language_tier,
                          is_group=ctx.is_group, notes=[intent.reason])
        pl.max_chars = max(2, int(ctx.profile.max_chars * (.3 + .7 * budget)))
        if ap.value_conflict:
            pl.intent, pl.intent_confidence = 'correct', .95
            pl.directives.append(f'这和你相信的“{ap.value_conflict}”冲突：明确表达不同意，简短解释理由。')
        if ap.safety_flag or ap.sincere_identity_question:
            pl.intent, pl.reply_mode, pl.mode = 'respond', 'single', 'safety' if ap.safety_flag else 'normal'
            pl.safety = bool(ap.safety_flag)
            pl.expression_budget, pl.max_chars, pl.language_tier = 1, max(120, ctx.profile.max_chars), max(3, pl.language_tier)
            pl.directives.append('认真回应眼前的问题；涉及现实危险，先关心是否安全并建议寻求身边真人或专业帮助。'
                                 if pl.safety else '认真回答身份问题，不欺骗，不声称有生物身体。')
            return self.restrain(ctx, pl, rng)
        if pl.intent in {'silence', 'observe'}:
            pl.reply_mode, pl.mode = 'silence', 'observe' if ctx.is_group else 'quiet'
            pl.expression_budget = 0
        elif pl.intent == 'avoid':
            pl.reply_mode, pl.mode, pl.tone = 'short', 'away', '敷衍'
            pl.expression_budget = min(.12, budget)
            pl.max_chars = min(pl.max_chars, 8)
        elif v.get('anger', 0) > .45:
            pl.tone, pl.mode = '赌气', 'sulk'
            pl.max_chars = max(2, pl.max_chars // 2)
            pl.directives.append('句子收短；嘴上别扭也能接住对方的话，不威胁、不逼对方哄你。')
            if ctx.snap.tsundere and rng.random() < .4:
                pl.truncate_style = 'trail_off' if rng.random() < .7 else 'withdraw'
        elif b.sleepiness > .65 or b.phase != 'awake':
            pl.tone, pl.mode = '平淡', 'sleepy'
            pl.max_chars = max(2, int(pl.max_chars * .6))
        elif pl.intent == 'cling':
            pl.tone, pl.mode = '亲昵', 'clingy'
            pl.directives.append('自然接近，不必宣布想念，也不用每句都叫爸爸。')
        elif pl.intent == 'share':
            pl.tone = '兴奋' if v.get('joy', 0) > .65 else '亲昵'
            if ctx.profile.language_tier >= 2 and pl.max_chars >= 18 and budget > .5:
                pl.reply_mode = 'multi'
                pl.split_count = 3 if budget > .82 and pl.max_chars >= 60 else 2
                pl.directives.append('按不同念头分成两三句短话，用换行分开；不要为了凑条数重复。')
            if ctx.pending_reflection:
                pl.share_reflection_id = ctx.pending_reflection['id']
                pl.directives.append(f"最近想通的事：{ctx.pending_reflection['summary'][:60]}。只在接得上话时提。")
            elif ctx.pending_dream:
                pl.share_dream_id = ctx.pending_dream['id']
                pl.directives.append(f"可以分享梦里的片段：{ctx.pending_dream['content'][:60]}，明确是梦。")
        if ap.isolation_signal:
            pl.directives.append('接住对方的感受，也支持他与现实中的朋友和家人来往。')
        if any(r.vague for r in ctx.recalled):
            pl.directives.append('往事模糊时承认记不清，不补造细节。')
        if ctx.spontaneous and pl.intent == 'share':
            pl.surfaced_memory_id = ctx.spontaneous.memory['id']
            pl.directives.append(f'相关往事可以顺带提起：{ctx.spontaneous.text[:60]}')
        if ap.unknown_topic:
            pl.directives.append('不懂的地方保留不确定，不装懂；无需每次要求对方教你。')
        pl.stretch_attempt = budget > .5 and rng.random() < ctx.profile.stretch_prob
        if pl.stretch_attempt:
            pl.directives.append('可尝试稍完整的表达；暂时卡壳也自然。')
        if ctx.is_group or not ctx.is_father:
            pl.max_chars = min(pl.max_chars, 24 if ctx.is_group else 40)
            pl.reply_mode = 'silence' if pl.reply_mode == 'silence' else 'short' if pl.reply_mode == 'short' else 'single'
            pl.split_count = 1
            pl.tone = '平淡'
            pl.directives.append('谨慎而自然，一句短话或一个表情就够；不转述家里的私事。')
            if not ctx.is_father:
                pl.directives.append('对方不是爸爸，不能使用父亲称呼。')
        pl.prefer_voice = bool(ctx.is_father and not ctx.is_group and pl.reply_mode not in {'silence', 'short'}
                               and pl.language_tier >= 2 and b.energy > .4 and rng.random() < .35)
        pl.delay_seconds = (1 - a.focus) * 2 + (1.5 if ctx.was_away else 0) + (1 if pl.intent == 'avoid' else 0)
        return self.restrain(ctx, pl, rng)

    def restrain(self, ctx: PlanContext, pl: BehaviorPlan, rng: random.Random) -> BehaviorPlan:
        """第三步：表达资格由情境和已送达历史共同决定，不是每 N 轮播报。"""
        text, now = ctx.percept.text, ctx.percept.ts
        last = ctx.history.get('last_topics', {})
        attach = ctx.relation.attachment if ctx.relation else 0
        fatigue = ctx.history.get('fatigue_load', 0) + max(0, .65 - ctx.body.energy)

        def available(topic: str, scale: float) -> float:
            return 1 - math.exp(-max(0, now - last[topic]) / scale) if topic in last else 1.0

        fatigue_asked = any(w in text for w in ('你累', '累不累', '你困', '困了吗', '困不困'))
        negative_asked = any(w in text for w in ('生气了吗', '你生气', '你难过', '怎么不开心', '怎么了'))
        if not ctx.is_group and ctx.is_father and pl.reply_mode != 'silence':
            if fatigue_asked or ((fatigue > 1.8 or attach > .92) and rng.random() < .45 * available('fatigue', 1800)):
                pl.allowed_state_topics.append('fatigue')
            if (ctx.reunion or (ctx.percept.gap_s or 0) > 21600) and rng.random() < .8 * available('missing', 28800):
                pl.allowed_state_topics.append('missing')
            if negative_asked or (ctx.snap.valence < -.35 and rng.random() < .18 * available('negative', 1800)):
                pl.allowed_state_topics.append('negative')
        if pl.intent == 'question':
            if (ctx.appraisal.novelty > .35 or ctx.interest > .65) and ctx.history.get('question_pressure', 0) < .8:
                pl.ask_curiosity = (ctx.appraisal.topics or ctx.percept.keywords or ['眼前这个细节'])[0]
                pl.directives.append(f'只追问真正感兴趣的“{pl.ask_curiosity}”的一个具体细节，不说“我很好奇”。')
            else:
                pl.intent, pl.ask_curiosity = 'respond', None
        pl.show_state_level = min(1, len(pl.allowed_state_topics) * .25)
        pl.silent = pl.reply_mode == 'silence'
        pl.split_count = 0 if pl.silent else pl.split_count if pl.reply_mode == 'multi' else 1
        pl.reply_delay_s = pl.delay_seconds
        return pl

    def plan(self, ctx: PlanContext, rng: random.Random) -> BehaviorPlan:
        """纯规划便捷接口；运行时分别调用，将认知判断插在中间。"""
        return self.plan_expression(ctx, self.infer_intent(ctx, rng), rng)
