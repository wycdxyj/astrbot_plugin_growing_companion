"""第三版行为契约：旁听不发言、上下文隔离、并发/重启限流、来源与价值讨论。"""
import asyncio
import random
import sqlite3

from growing_companion.core.db import Database, SCHEMA
from growing_companion.core.engine import CompanionEngine
from growing_companion.core.models import EmotionSnapshot
from growing_companion.core.planner import BehaviorPlanner

from .conftest import Env, FakeLLM, fingerprint, run


class QuietRandom(random.Random):
    def random(self):
        return 0.999


class EagerRandom(random.Random):
    def random(self):
        return 0.0


async def group(env, text='你好', actor='qq:u', directed=False, scope='qq:GroupMessage:g', mid=''):
    return await env.engine.handle_message(actor, text, scope_id=scope, is_group=True, directed=directed,
                message_id=mid, now=env.t, spawn_reflection=False)


def test_unprompted_bubble_can_follow_curiosity(env):
    env.engine.rng = EagerRandom()
    async def go():
        reply = await group(env, '小满，我们刚刚聊到“新发现”这个词。', directed=False)
        assert not reply.silent and len(env.llm.calls) == 1
        await env.engine.social.complete(reply, True)
    run(go())


def test_observation_learns_without_model_reply_or_practice(env):
    env.engine.rng = QuietRandom()
    before = sum(r['practice_count'] for r in env.engine.growth.caps().values())
    result = run(group(env, '最近新闻发布了“星际旅行”这个新词，谢谢你告诉我。', mid='one'))
    assert result.silent and not result.text and not env.llm.calls
    rows = env.engine.db.query('SELECT * FROM social_cognition')
    assert rows and all(r['confidence'] == 0.3 for r in rows)
    assert sum(r['practice_count'] for r in env.engine.growth.caps().values()) == before
    assert not env.engine.is_father('qq:u')
    fp = fingerprint(env.db_path)
    run(group(env, '最近新闻发布了“星际旅行”这个新词，谢谢你告诉我。', mid='one'))
    assert fingerprint(env.db_path) == fp


def test_scope_isolation_includes_link_expansion_and_compressed_parent(env):
    mem = env.engine.memory
    private = mem.add(mtype='relational', content='爸爸的私聊密码火星', now=env.t, importance=0.9)
    a = mem.add(mtype='episodic', content='火星探索讨论', now=env.t, importance=0.9, scope_id='group:a')
    other = mem.add(mtype='semantic', content='另一群秘密火星', now=env.t, importance=0.9, scope_id='group:b')
    mem.link(a, private, 'context', 1)
    mem.link(a, other, 'context', 1)
    found = mem.retrieve('火星', ['火星'], env.t, 20, scope_id='group:a')
    assert {r.memory['id'] for r in found} == {a}
    env.engine.db.execute('UPDATE memories SET compressed=1,compressed_into=? WHERE id=?', (private, a))
    assert not mem.retrieve('火星', ['火星'], env.t, 20, scope_id='group:a')
    assert mem.compress_group([a, other], '跨群压缩', env.t) is None


def test_group_context_does_not_contain_father_history(env):
    async def go():
        await env.say('我有一个秘密，保险箱密码是 SECRETDAD', gap=1)
        result = await group(env, '请聊聊 SECRETDAD', directed=True)
        assert not result.silent
        call = env.llm.calls[-1]
        assert '保险箱密码' not in repr(call)
        assert '对方是陌生人' in call[2]
        assert '爸爸' not in result.text
        await env.engine.social.complete(result, True)
    run(go())


def test_concurrent_direct_mentions_only_one_generation(env):
    async def go():
        replies = await asyncio.gather(*(group(env, directed=True, mid=str(i)) for i in range(8)))
        sent = [r for r in replies if not r.silent]
        assert len(sent) == 1 and len(env.llm.calls) == 1
        await env.engine.social.complete(sent[0], True)
        assert (await group(env, directed=True)).silent
        env.advance(100)
        later = await group(env, directed=True)
        assert not later.silent
        await env.engine.social.complete(later, True)
    run(go())


def test_global_cooldown_and_hour_limit(env):
    social = env.engine.social
    env.engine.cfg['social_hourly_limit'] = 1
    first = social.reserve('a', env.t, True)
    assert first and not social.reserve('b', env.t, True)
    social.release('a', first, env.t, True)
    assert not social.reserve('a', env.t + 100, True)  # 不能靠等待 CD 绕过小时上限。
    assert social.reserve('b', env.t + 100, True)


def test_cooldown_survives_restart_and_atomic_two_connections(env):
    token = env.engine.social.reserve('a', env.t, True)
    second = CompanionEngine(env.db_path, {'father_ids': ['dad']}, clock=env.clock)
    try:
        assert not second.social.reserve('a', env.t, True)
        env.engine.social.release('a', token, env.t, True)
        assert not second.social.reserve('a', env.t + 5, True)
        assert second.social.reserve('a', env.t + 100, True)
    finally:
        second.close()


def test_send_failure_does_not_record_spoken_words_or_practice(env):
    async def go():
        before = sum(r['practice_count'] for r in env.engine.growth.caps().values())
        result = await group(env, directed=True)
        await env.engine.social.complete(result, False)
        assert env.engine.db.query_one("SELECT COUNT(*) n FROM conversation_log WHERE role='assistant'")['n'] == 0
        assert sum(r['practice_count'] for r in env.engine.growth.caps().values()) == before
        assert (await group(env, directed=True)).silent
    run(go())


def test_model_failure_keeps_cooldown_and_stays_silent(env):
    class Broken:
        async def chat(self, **kwargs):
            raise RuntimeError('test failure')
    env.engine.llm = Broken()
    result = run(group(env, directed=True))
    assert result.silent and not env.engine.social.pending
    assert not env.engine.social.reserve('qq:GroupMessage:g', env.t + 1, True)


def test_ignored_speech_reduces_probability_and_grows_cooldown(env):
    async def go():
        result = await group(env, directed=True)
        await env.engine.social.complete(result, True)
    run(go())
    env.advance(181)
    frustration = env.engine.social.feedback('qq:GroupMessage:g', env.t, False)
    assert frustration > 0
    args = dict(directed=False, mentioned=True, role='familiar', novelty=0.5, resonance=0.7,
                interest=0.8, share=1, energy=0.8, curiosity=0.7, introversion=0.3)
    p0 = BehaviorPlanner.social_decision(**args, frustration=0, rng=random.Random(1)).probability
    p1 = BehaviorPlanner.social_decision(**args, frustration=frustration, rng=random.Random(1)).probability
    assert p1 < p0
    # 群里的无关聊天不视作回应。
    assert env.engine.db.query_one('SELECT unanswered FROM speech_cooldowns WHERE scope_id=?', ('qq:GroupMessage:g',))['unanswered'] == 1


def test_familiarity_requires_interactions_and_time(env):
    social = env.engine.social
    for i in range(20):
        r = social.relation('qq:friend', env.t + i * 400, True, {'praise'})
    assert r['role'] == 'familiar'
    for i in range(100):
        stranger = social.relation('qq:watcher', env.t + i * 400, False, set())
    assert stranger['role'] == 'stranger' and stranger['trust'] == 0.2


def test_no_first_contact_auto_father_binding(tmp_path):
    e = CompanionEngine(str(tmp_path / 'test.db'))
    try:
        assert not e.resolve_father('stranger', 1)
        assert e.db.kv_get('father_id') is None
    finally:
        e.close()


def test_reflection_and_father_guidance_keep_originals(env):
    env.engine.rng = QuietRandom()
    run(group(env, '有人说骗人也没关系，这样真的对吗？'))
    social = env.engine.social
    before = env.engine.db.query('SELECT id,content FROM memories')
    social.reflect('qq:GroupMessage:g', env.t, 0.9)
    item = social.guidance('今天你学到什么了', env.t)
    assert item and item['topic'] == '诚实与欺骗'
    social.mark_guidance_offered(item, '爸爸，我想问诚实与欺骗该怎么看？', env.t)
    social.guidance('我觉得不应该骗人，因为别人会受伤。', env.t + 60)
    row = env.engine.db.query_one('SELECT * FROM social_cognition WHERE id=?', (item['id'],))
    assert row['discussion_state'] == 'discussed' and row['father_view']
    assert row['confidence'] == 0.3  # 父亲观点也不会被错误计为事实验证。
    for old in before:
        assert env.engine.memory.get(old['id'])['content'] == old['content']


def test_upgrade_v1_schema_preserves_original_content(tmp_path):
    path = tmp_path / 'old.db'
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO memories(type,content,created_at,last_touched_at) VALUES('episodic','旧版原文',1,1)")
    con.commit()
    con.close()
    db = Database(path)
    try:
        assert db.query_one('SELECT * FROM memories')['content'] == '旧版原文'
        assert db.query_one('SELECT * FROM memories')['scope_id'] == 'legacy:father'
        assert db.query_one('PRAGMA user_version')['user_version'] == 4
        db2 = Database(path)
        db2.close()
    finally:
        db.close()
