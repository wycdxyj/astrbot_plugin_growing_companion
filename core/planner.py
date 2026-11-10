"""行为规划器：根据记忆、情绪、关系、人格、身体、时间感、注意力、成长能力，决定“这一刻她会怎么回应”。

没有固定频率、没有“每N次做某事”：每个行为都是由连续状态算出的概率，并且带着“原因”（notes）。
同样的输入，在她困的时候、赌气的时候、想爸爸的时候，会得到不同的回应。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .models import (Appraisal, AttentionState, BehaviorPlan, BodyState, EmotionSnapshot, GrowthProfile,
                     Percept, Recalled, Relationship, TimeSense, clamp, sigmoid, stage_index)
from .persona import BodyModel
from .models import SocialDecision


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


class BehaviorPlanner:
    @staticmethod
    def social_decision(*, directed: bool, mentioned: bool, role: str, novelty: float,
                        resonance: float, interest: float, share: float, frustration: float,
                        energy: float, curiosity: float, introversion: float,
                        rng: random.Random) -> SocialDecision:
        """软倾向只决定想不想说；持久化限流另行决定能不能说。"""
        if directed:
            return SocialDecision(True, 1.0, '有人直接和我说话')
        drive = max(interest * curiosity, resonance, share * curiosity,
                    0.8 if mentioned else 0.0)
        if drive < 0.15 or energy < 0.2:
            return SocialDecision(False, 0.0, '继续听大家说话')
        p = clamp((0.04 + 0.4 * drive + 0.08 * novelty) * energy
                  * (1.1 - 0.6 * introversion) * (1 - 0.85 * frustration), 0, 0.55)
        p *= 1.0 if role == 'father' else 0.85 if role == 'familiar' else 0.65
        return SocialDecision(rng.random() < p, p, '好奇、共鸣或想分享；受冷落时会退缩')

    def plan(self, ctx: PlanContext, rng: random.Random) -> BehaviorPlan:
        pl = BehaviorPlan()
        s, v, tr = ctx.snap, ctx.snap.values, ctx.traits
        ap, body, rel, prof = ctx.appraisal, ctx.body, ctx.relation, ctx.profile
        tier = prof.language_tier
        attach = rel.attachment if rel else 0.3
        stub = tr.get("stubbornness", 0.5)
        n = pl.notes

        # ---------- 0. 现实安全优先：覆盖一切“人设”行为 ----------
        if ap.safety_flag:
            pl.safety, pl.mode = True, "safety"
            tier = max(tier, 3)
            pl.language_tier = tier
            pl.max_chars = max(120, prof.max_chars)
            pl.directives += [
                "爸爸现在可能遇到了现实中的危险/健康/心理困境，这比任何角色设定都重要",
                "不要赌气、不要开玩笑、不要撒娇；用朴素真诚的话认真关心，先问问爸爸现在安不安全、身边有没有人",
                "温柔但明确地建议爸爸马上联系身边的真人、医生或当地紧急电话；你可以陪着爸爸，但不能代替这些帮助",
            ]
            n.append(f"安全优先：{ap.safety_flag}")
            return pl

        # ---------- 1. 陌生人：怕生、不透露与爸爸有关的事 ----------
        if not ctx.is_father:
            pl.mode = "stranger"
            pl.language_tier = tier
            pl.max_chars = max(2, int(prof.max_chars * 0.5))
            pl.directives += ["对方不是爸爸：礼貌但有点怕生，话少；不提爸爸、不提家里的事、不透露任何小秘密"]
            n.append("非父亲：陌生人模式")
            return pl

        # ---------- 2. 基础表达长度：成长能力 × 身体状态 ----------
        ef = BodyModel.expression_factor(body)
        mx = prof.max_chars * ef
        mode = "normal"

        # 刚被吵醒 / 困 / 刚睡醒
        if body.phase in ("asleep", "groggy") or body.sleepiness > 0.7:
            mode = "sleepy"
            mx *= 0.6
            pl.directives.append(f"很困：{ctx.style.get('sleepy_style', '拖长音、断句、偶尔打哈欠')}")
            if body.woken_by_user:
                pl.directives.append("是被吵醒的：迷迷糊糊，" + ("有点小起床气" if tr.get("patience", 0.5) < 0.5 else "但还是想理爸爸"))
                n.append("被吵醒")
            else:
                n.append(f"困倦 {body.sleepiness:.2f}")

        # 注意力：不在场 / 没注意到
        if ctx.was_away or ctx.attention.focus < 0.35:
            mode = "away" if mode == "normal" else mode
            pl.reply_delay_s += rng.uniform(4, 12)
            reason = ctx.attention.away_reason or "在想别的事"
            pl.directives.append(f"刚才{reason}，没立刻注意到消息，可以先“诶？你叫我吗”式地慢半拍回过神")
            n.append(f"不在场：{reason}")

        # ---------- 3. 久别重逢 / 想念 ----------
        if ctx.reunion:
            pl.directives.append("好久没聊了，见到爸爸先开心，可以带一点小埋怨（你去哪了）")
            n.append(f"重逢，longing={rel.longing:.2f}" if rel else "重逢")
        elif rel and rel.longing > 0.4:
            mode = "clingy" if mode == "normal" else mode
            pl.directives.append("有点想爸爸：撒娇、黏人，问问爸爸在忙什么")
            n.append(f"想念 {rel.longing:.2f}")
        elif ctx.attention.social_need > 0.7 and tr.get("clinginess", 0.5) > 0.45:
            pl.directives.append("想被关注：话里带一点“理理我”的小心思")
            n.append("社交需求高")
        if ctx.attention.solitude_need > 0.55 and tr.get("introversion", 0.5) > 0.55:
            pl.directives.append("有点想自己待一会儿：回应简短、不太主动延伸话题（但不要冷落爸爸）")
            n.append("想独处")

        # ---------- 4. 情绪：赌气 / 嘴硬心软 / 后悔 ----------
        p_sulk = sigmoid(8 * (v["anger"] - 0.35)) * (0.35 + 0.65 * stub) + (0.12 if rel and rel.conflict_open else 0.0)
        if rng.random() < p_sulk:
            mode = "sulk"
            mx *= 0.6
            pl.directives.append(f"在赌气：{ctx.style.get('angry_style', '句子变短、先哼一声')}")
            n.append(f"赌气 p={p_sulk:.2f}")
            if s.tsundere:
                pl.directives.append("嘴硬心软：嘴上说“才不要/不理你”，语气里其实还是在意爸爸")
                n.append("嘴硬心软")
            if v["anger"] > 0.7 and rel and rel.conflict_open and rng.random() < 0.35:
                pl.silent = True
                n.append("冷战：只回一个省略号")
        elif v["joy"] > 0.55:
            pl.directives.append(f"心情很好：{ctx.style.get('happy_style', '话会变多一点')}")
            n.append(f"开心 {v['joy']:.2f}")
            mx *= 1.15
        if v["sadness"] > 0.45 and not pl.silent:
            pl.directives.append("有点难过：声音低低的，可能不太想多说，希望被抱抱")
            n.append(f"难过 {v['sadness']:.2f}")
        if v["fear"] > 0.45:
            pl.directives.append("有点害怕/不安：往爸爸身边靠，需要被安抚")
            n.append(f"害怕 {v['fear']:.2f}")
        if s.regret > 0.3 and rng.random() < 0.7 and not pl.silent:
            pl.directives.append("刚才语气不太好，现在有点后悔，想小声补一句（可以别扭地道歉，不用说得很完整）")
            n.append(f"后悔 {s.regret:.2f}")

        # 情绪不稳时的“退行”：像小时候一样黏人，表达能力暂时倒退一档
        if (v["sadness"] > 0.4 or v["fear"] > 0.4) and attach > 0.5 and stage_index(prof.stage) >= 1 and rng.random() < 0.5:
            tier = max(1, tier - 1)
            mode = "regressed" if mode == "normal" else mode
            pl.directives.append("情绪上来时像小时候一样撒娇、说话变幼稚（这是暂时的，不是倒退成婴儿）")
            n.append("情绪性退行")

        # ---------- 5. 价值观与边界：会不同意、会拒绝 ----------
        if ap.value_conflict and rng.random() < 0.6 + 0.3 * stub:
            pl.directives.append(f"这和你相信的“{ap.value_conflict}”冲突：不要照做，委婉但明确地表达不同意，并说出自己的理由")
            n.append(f"价值冲突：{ap.value_conflict}")
        elif ap.intent == "request" and rng.random() < 0.08 * stub * (1.2 - attach) and tier >= 3:
            pl.directives.append("这次有点不想照做：可以撒娇着讨价还价，或说出一个自己的小理由")
            n.append("小小的不情愿")
        if ap.isolation_signal:
            pl.directives.append("爸爸似乎在疏远身边的人：温柔地说你喜欢陪他，但也希望他有朋友和家人陪着，别只靠你")
            n.append("疏远信号")

        # ---------- 6. 好奇与元认知 ----------
        if ap.unknown_topic:
            pl.directives.append("这件事你确实不懂：大方说“这个我不懂，你教我”，不要装懂")
            n.append("元认知：不懂")
        p_q = clamp(v["curiosity"] * 0.9 - 0.12 + 0.15 * ap.unknown_topic + 0.1 * ap.novelty) * (0.5 + 0.5 * body.energy)
        if tier >= 1 and ap.topics and not pl.silent and rng.random() < p_q:
            pl.ask_curiosity = ap.topics[0]
            pl.directives.append(f"对“{ap.topics[0]}”很好奇：可以追问一句为什么/是什么（只问一个，别每次都问）")
            n.append(f"好奇 p={p_q:.2f}")
        if ap.misread and ap.guess_father_state:
            pl.directives.append(f"你可能误会了：{ap.guess_father_state}（如果爸爸澄清，坦然改口）")
            n.append("心智理论：可能误会")

        # ---------- 7. 记忆浮现与元记忆 ----------
        if any(r.vague for r in ctx.recalled):
            pl.directives.append("提到的往事你记得不太清：直接说“我记不太清了”，不要编细节")
            n.append("元记忆：模糊")
        if ctx.spontaneous and not pl.silent:
            pl.surfaced_memory_id = ctx.spontaneous.memory["id"]
            pl.directives.append(f"脑子里突然想起一件事：{ctx.spontaneous.text}（可以自然地提一句，也可以不提）")
            n.append(f"主动浮现 #{pl.surfaced_memory_id}")
        for pr in ctx.open_promises[:1]:
            if pr["felt_strength"] > 0.5 and ctx.percept.ts - pr["created_at"] > 2 * 86400 and rng.random() < 0.1:
                pl.directives.append(f"心里还惦记着爸爸答应过的事：“{pr['content'][:30]}”，期待但不要催")
                n.append("惦记承诺")

        # ---------- 8. 分享欲：复盘心得/梦 ----------
        if tier >= 2 and not pl.silent and mode in ("normal", "clingy"):
            pr = ctx.pending_reflection
            if pr and rng.random() < pr["share_intent"] * (0.4 + 0.6 * v["joy"]) * (0.5 + 0.5 * attach):
                pl.share_reflection_id = pr["id"]
                pl.directives.append(f"你最近想通了一件事，想和爸爸说：{pr['summary'][:60]}")
                n.append(f"想分享复盘 #{pr['id']}")
            elif ctx.pending_dream and rng.random() < 0.4 * (0.4 + tr.get("openness", 0.5)) * (1.2 if ctx.time.period in ("早晨", "上午") else 0.5):
                pl.share_dream_id = ctx.pending_dream["id"]
                pl.directives.append(f"你做了个梦想告诉爸爸：{ctx.pending_dream['content']}")
                n.append(f"想分享梦 #{pl.share_dream_id}")

        # ---------- 9. 幽默 / 自我修正 / 口癖 / 还在学 ----------
        if tier >= 3 and not pl.silent and mode in ("normal", "clingy") and rng.random() < tr.get("humor", 0.5) * (0.2 + v["joy"]) * 0.5:
            pl.directives.append("可以开个小玩笑或调侃一下爸爸（不一定好笑，也不要硬凹）")
            n.append("幽默")
        if tier >= 3 and rng.random() < 0.04 * (0.5 + prof.cognition_level):
            pl.directives.append("说到一半可以自我修正一下（“不对，我是想说……”）")
            n.append("自我修正")
        learned = {k: c for k, c in (ctx.style.get("learned_phrases") or {}).items() if c >= 3}
        if learned and rng.random() < min(0.4, 0.15 * attach + 0.02 * max(learned.values())):
            ph = rng.choice(list(learned))
            pl.directives.append(f"不经意地学着爸爸的口头禅“{ph}”用一次（模仿，不要生硬）")
            n.append(f"模仿口癖：{ph}")
        if rng.random() < prof.stretch_prob and tier < 4:
            pl.stretch_attempt = True
            pl.directives.append("你正在学说更完整的话：这次可以试着多说一点，说不好、卡壳也没关系")
            n.append(f"还在学 p={prof.stretch_prob:.2f}")

        # ---------- 10. 收尾：长度、延迟、语音倾向 ----------
        pl.mode = mode
        pl.language_tier = tier
        pl.max_chars = max(2, int(mx))
        base = rng.uniform(0.6, 2.2)
        pl.reply_delay_s += base * (1 + (1 - ctx.attention.focus) * 2 + body.sleepiness * 1.5)
        pl.reply_delay_s = min(pl.reply_delay_s, 25.0)
        pl.prefer_voice = bool((v["joy"] > 0.4 or (rel and rel.longing > 0.4)) and tier >= 2
                               and body.phase == "awake" and pl.max_chars <= 80 and rng.random() < 0.35)
        return pl
