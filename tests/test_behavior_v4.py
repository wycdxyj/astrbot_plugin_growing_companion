"""4.0 行为验收：长期节制、真实沉默、分条投递、失败回执与人格修复。"""
import asyncio
import random

from growing_companion.core.consistency import disclosed_topics
from growing_companion.core.db import Database
from growing_companion.core.expression import ReplySender, SessionGate, shape_reply
from growing_companion.core.models import (Appraisal, AttentionState, BehaviorPlan, BodyState,
    ContextUsage, EmotionSnapshot, EngineReply, GrowthProfile, IntentDecision, Percept, Relationship, TimeSense)
from growing_companion.core.planner import BehaviorPlanner, PlanContext

from .conftest import FakeLLM, run


def context(now=1000000):
    return PlanContext(
        percept=Percept('刚才的小猫跑到花盆后面了。', 'dad', now, keywords=['小猫']),
        appraisal=Appraisal(topics=['小猫'], novelty=.7),
        snap=EmotionSnapshot(dict(joy=.4, trust=.8, fear=.1, sadness=.1, curiosity=.7, anger=.05),
                             dominant='trust', arousal=.4),
        body=BodyState(energy=.8, sleepiness=.15), attention=AttentionState(), was_away=False,
        relation=Relationship('dad', attachment=.8), traits={'introversion': .5}, style={'name': '小满'},
        profile=GrowthProfile('young_adult', 4, '复杂表达', 160, .2, .8, '有自己的想法'),
        time=TimeSense(now, 14, '周三', '下午', False, 60, '刚刚'), is_father=True)


def test_ten_turns_do_not_broadcast_every_state(env):
    async def go():
        spoken = []
        for index in range(10):
            now = env.t + index * 60
            ctx = context(now)
            ctx.body.energy = .4  # 连续有疲惫，但不是每轮都得说。
            ctx.history = env.engine.behavior.history('test', 'dad', now)
            plan = env.engine.planner.plan_expression(ctx, IntentDecision('respond', .9), random.Random(index))
            bad_model = FakeLLM(f'我累了。我想你了。我很好奇。小猫又躲在第{index}个花盆后面啦。')
            def assemble(extra):
                return env.engine.budget.assemble(fixed_key='test', fixed_full=lambda: '日常口语。',
                    fixed_short=lambda: '日常口语。', state_segs=extra, retrieval_segs=[], dialog=[], user_text=ctx.percept.text)
            text, usage = await env.engine.generate(assemble, plan, ctx.appraisal, ctx.snap, bad_model, ctx.history, now)
            assert usage.usage.total <= 5000
            assert len(bad_model.calls) <= 2
            parts = shape_reply(text, plan)
            bid = env.engine.behavior.record('test', 'dad', plan, now, ctx.history['fatigue_load'] + .6)
            env.engine.behavior.complete(bid, parts, now, expected=len(parts))
            spoken.append('\n'.join(parts))
        assert sum('fatigue' in disclosed_topics(t) for t in spoken) <= 3
        assert all('missing' not in disclosed_topics(t) and 'curiosity' not in disclosed_topics(t) for t in spoken)
        assert all('小猫' in t for t in spoken)
    run(go())


def test_unwilling_can_be_short_or_genuinely_silent():
    ctx = context()
    ctx.body.energy, ctx.body.sleepiness = .04, .95
    ctx.attention.focus, ctx.attention.solitude_need = .1, .95
    modes = {BehaviorPlanner().plan(ctx, random.Random(seed)).reply_mode for seed in range(15)}
    assert {'short', 'silence'} <= modes
    for mode in modes:
        plan = BehaviorPlan(reply_mode=mode, max_chars=80)
        parts = shape_reply('这是一段本来会说很长很长的话。', plan)
        assert parts == [] if mode == 'silence' else len(''.join(parts)) <= 8


def test_excited_sharing_splits_and_waits_between_messages():
    ctx = context()
    ctx.snap.values['joy'], ctx.snap.arousal = .95, .9
    ctx.body.energy = .95
    plan = BehaviorPlanner().plan(ctx, random.Random(1))
    assert plan.intent == 'share' and plan.reply_mode == 'multi'
    parts = shape_reply('爸爸快看，小猫自己爬上去了！它还把花盆推歪啦！我刚才差点没看清。', plan)
    assert 2 <= len(parts) <= 3
    events, delivered = [], []
    async def sleep(delay): events.append(('delay', delay))
    async def send(text): events.append(('text', text))
    reply = EngineReply('\n'.join(parts), plan, ContextUsage(), parts=parts, delay_s=plan.delay_seconds)
    run(ReplySender(sleep=sleep).send(reply, send_text=send, send_voice=send, delivered=delivered))
    assert delivered == parts
    assert [kind for kind, _ in events] == ['delay', 'text'] * len(parts)
    assert all(value > 0 for kind, value in events if kind == 'delay')


def test_group_lurking_is_absolute_and_bubbles_are_brief():
    ctx = context()
    ctx.is_group, ctx.social_admitted = True, False
    planner = BehaviorPlanner()
    plan = planner.plan(ctx, random.Random(3))
    assert plan.intent == 'observe' and shape_reply('不要发送这段', plan) == []
    ctx.social_admitted = True
    ctx.snap.values['joy'], ctx.snap.arousal = .99, .9
    plan = planner.plan(ctx, random.Random(3))
    parts = shape_reply('啊' * 100 + '。' + '哦' * 100, plan)
    assert len(parts) == 1 and len(parts[0]) <= 24 and not plan.prefer_voice


def test_partial_delivery_only_commits_sent_content(env, monkeypatch):
    env.engine.llm = FakeLLM('小猫爬上去了！花盆也歪啦！')
    monkeypatch.setattr(env.engine.planner, 'plan_expression', lambda *args: BehaviorPlan(
        intent='share', reply_mode='multi', split_count=2, tone='兴奋', max_chars=100, language_tier=3))
    async def go():
        reply = await env.engine.handle_message('dad', '给我讲讲小猫吧', spawn_reflection=False)
        assert len(reply.parts) == 2
        before = env.engine.db.query_one("SELECT COUNT(*) n FROM conversation_log WHERE role='assistant'")['n']
        sent = []
        async def send(text):
            if sent:
                raise RuntimeError('第二条平台失败')
        async def sleep(seconds): return None
        await ReplySender(sleep=sleep).send(reply, send_text=send, send_voice=send, delivered=sent)
        await env.engine.complete_reply(reply, sent)
        rows = env.engine.db.query("SELECT content FROM conversation_log WHERE role='assistant'")
        assert len(rows) == before + 1 and rows[-1]['content'] == reply.parts[0]
        row = env.engine.db.query_one('SELECT * FROM expression_history WHERE id=?', (reply.behavior_id,))
        assert row['status'] == 'partial' and row['delivered_text'] == reply.parts[0]
        await env.engine.complete_reply(reply, sent)
        assert env.engine.db.query_one("SELECT COUNT(*) n FROM conversation_log WHERE role='assistant'")['n'] == before + 1
    run(go())


def test_silence_skips_model_sender_voice_and_practice(env, monkeypatch):
    monkeypatch.setattr(env.engine.planner, 'infer_intent', lambda *args: IntentDecision('silence', .95))
    async def go():
        before = {key: c['practice_count'] for key, c in env.engine.growth.caps().items()}
        reply = await env.engine.handle_message('dad', '今天天气很晴朗。', spawn_reflection=False)
        assert reply.silent and not reply.text and not env.llm.calls
        async def unexpected(*args): raise AssertionError('沉默不能调用发送/TTS')
        await ReplySender(sleep=unexpected).send(reply, send_text=unexpected, send_voice=unexpected, delivered=[])
        await env.engine.complete_reply(reply, [])
        assert not env.engine.db.query("SELECT 1 FROM conversation_log WHERE role='assistant'")
        assert {key: c['practice_count'] for key, c in env.engine.growth.caps().items()} == before
    run(go())


def test_consistency_repairs_customer_service_once(env, monkeypatch):
    class Drifting:
        def __init__(self): self.calls = []
        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            return '很高兴为您服务。我想你了。' if len(self.calls) == 1 else '那只猫真的好小。'
    model = Drifting()
    monkeypatch.setattr(env.engine.planner, 'plan_expression', lambda *args: BehaviorPlan(max_chars=60))
    async def go():
        reply = await env.engine.handle_message('dad', '小猫就蹲在那里', llm=model, spawn_reflection=False)
        assert [c['purpose'] for c in model.calls] == ['reply', 'persona_repair']
        assert '客服' in model.calls[-1]['system_prompt']
        assert reply.text == '那只猫真的好小。'
        assert reply.plan.persona_drift_score >= .7 and reply.plan.last_consistency_check
        await env.engine.complete_reply(reply, reply.parts)
    run(go())


def test_exposure_survives_database_reopen(env):
    bid = env.engine.behavior.record('test', 'dad', BehaviorPlan(), env.t, 2)
    env.engine.behavior.complete(bid, ['我累了。'], env.t)
    from growing_companion.core.behavior_state import BehaviorStore
    db = Database(env.db_path)
    try:
        assert db.query_one('PRAGMA user_version')['user_version'] == 4
        assert BehaviorStore(db).history('test', 'dad', env.t + 1)['last_topics']['fatigue'] == env.t
    finally:
        db.close()


def test_session_order_and_lock_cleanup():
    gate = SessionGate()
    timeline = []
    async def task(value):
        async with gate.hold('room'):
            timeline.append(value)
            await asyncio.sleep(0)
            timeline.append(value)
    async def go(): await asyncio.gather(task(1), task(2))
    run(go())
    assert timeline == [1, 1, 2, 2] and not gate._locks


def test_cancelling_multi_send_preserves_first_receipt():
    sent = []
    async def send(text): return None
    async def sleep(seconds):
        if sent:
            raise asyncio.CancelledError()
    reply = EngineReply('第一句。\n第二句。', BehaviorPlan(reply_mode='multi', split_count=2),
                        ContextUsage(), parts=['第一句。', '第二句。'], delay_s=1)
    async def go():
        try:
            await ReplySender(sleep=sleep).send(reply, send_text=send, send_voice=send, delivered=sent)
        except asyncio.CancelledError:
            return
        raise AssertionError('取消应传播给宿主')
    run(go())
    assert sent == ['第一句。']


def test_curiosity_does_not_force_a_followup_on_every_topic(env):
    from growing_companion.core.consistency import PersonaConsistency
    ctx = context()
    ctx.interest = .1
    ctx.history = {'question_pressure': 1.6}
    plan = BehaviorPlanner().plan(ctx, random.Random(6))
    assert plan.ask_curiosity is None
    guard = PersonaConsistency()
    text = '小猫躲进去了。为什么它这样做？'
    assert guard.check(text, plan, ctx.history).score >= .4
    assert guard.clean(text, plan, ctx.history) == '小猫躲进去了。'


def test_interruption_changes_shape_without_padding():
    plan = BehaviorPlan(max_chars=40, truncate_style='withdraw')
    parts = shape_reply('我刚才是想把那本故事书拿给你看看。', plan)
    assert len(parts) == 1 and parts[0].endswith('算了。') and len(parts[0]) <= 40
