"""AstrBot 入口和预算的外部行为测试；模型供应商使用明确的离线桩。"""
import importlib
import sys
import types

from growing_companion.core.budget import ContextBudgetManager, estimate_tokens

from .conftest import fingerprint, run
from .test_main_commands import FakeEvent, _install_stub
from .test_social_v3 import QuietRandom


class GroupEvent(FakeEvent):
    def __init__(self, uid='u', text='普通的聊天', directed=False, platform='qq-main'):
        super().__init__(uid)
        self.platform_meta = types.SimpleNamespace(id=platform)
        self.message_obj = types.SimpleNamespace(message_id='')
        self.message_str = text
        self.unified_msg_origin = platform + ':GroupMessage:g'
        self.is_at_or_wake_command = directed
        self.sent = []

    def is_private_chat(self):
        return False

    def get_self_id(self):
        return 'bot'

    async def send(self, message):
        self.sent.append(message)


class Context:
    def __init__(self):
        self.calls = 0

    async def get_current_chat_provider_id(self, *, umo):
        return 'configured-provider'

    async def llm_generate(self, *, chat_provider_id, prompt, system_prompt, contexts):
        self.calls += 1
        return types.SimpleNamespace(completion_text='你好，我也觉得这个词很有意思。')


def plugin_at(tmp_path, monkeypatch):
    _install_stub()
    sys.modules.pop('growing_companion.main', None)
    main = importlib.import_module('growing_companion.main')
    monkeypatch.chdir(tmp_path)
    plugin = main.GrowingCompanion(Context(), {'father_ids': ['qq-main:dad'], 'brainmap_enabled': False,
                'persona_init': {'initial_stage': 'child'}, 'human_like_delay': False})
    plugin.engine.rng = QuietRandom()
    return plugin


def test_passive_adapter_does_not_reply_or_stop_other_plugins(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch)

    async def go():
        observer = GroupEvent(text='有人提到了“新词语”，我们还不知道是什么意思。')
        assert [r async for r in plugin.on_message(observer)] == []
        assert not observer.sent and not observer.stopped and plugin.context.calls == 0
        assert not observer.call_llm  # 禁止宿主默认 LLM 抢答；其他插件仍可继续观察。
        called = GroupEvent(text='你怎么看这个词？', directed=True)
        assert [r async for r in plugin.on_message(called)] == []
        assert len(called.sent) == 1 and called.stopped and plugin.context.calls == 1
        repeated = GroupEvent(directed=True)
        assert [r async for r in plugin.on_message(repeated)] == []
        assert not repeated.sent and repeated.stopped and plugin.context.calls == 1
        self_message = GroupEvent(uid='bot', directed=True)
        assert [r async for r in plugin.on_message(self_message)] == []
        assert not self_message.sent
        await plugin.terminate()
    run(go())


def test_yield_replies_reach_send_stage_before_stopping_event(tmp_path, monkeypatch):
    """对应 v4.29.4 PipelineScheduler: 每次 yield 后先检查 is_stopped，才继续发送。"""
    plugin = plugin_at(tmp_path, monkeypatch)
    class Result:
        def __init__(self, text):
            self.text, self.stopped = text, False
        def stop_event(self):
            self.stopped = True
            return self

    async def go():
        for text in ('/她状态', '/记忆详情 1', '/最近复盘', '爸爸来啦，你真乖'):
            ev = FakeEvent('qq-main:dad')
            ev.message_str = text
            ev.plain_result = Result
            sent = []
            async for result in plugin.on_message(ev):
                assert not ev.stopped and not result.stopped, '结果被提前停止，AstrBot 会跳过发送阶段'
                sent.append(result.text)
            if text.startswith('/'):
                assert sent and not ev.sent and ev.stopped
            else:
                assert not sent and ev.sent and ev.stopped  # 4.0 普通回复直接 await send，按回执记录。
        await plugin.terminate()
    run(go())


def test_first_readonly_commands_never_initialize_write_or_leak_into_group(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch)

    async def go():
        before = fingerprint(str(plugin.data_dir / 'companion.db'))
        for text in ('/她状态', '/记忆列表', '/记忆详情 1', '/成长报告', '/关系状态', '/最近复盘', '/脑图'):
            ev = FakeEvent('qq-main:dad')
            ev.message_str = text
            assert [r async for r in plugin.on_message(ev)]
        assert fingerprint(str(plugin.data_dir / 'companion.db')) == before
        assert not plugin._started
        in_group = GroupEvent(uid='dad', text='/脑图', directed=True)
        assert [r async for r in plugin.on_message(in_group)] == [] and not in_group.sent
        assert fingerprint(str(plugin.data_dir / 'companion.db')) == before
        assert not plugin._allowed(GroupEvent(uid='dad', platform='other-bot'))
        await plugin.terminate()
    run(go())


def test_budget_counts_serialized_layers_and_handles_unicode():
    for total in (3000, 4000, 5000):
        budget = ContextBudgetManager(total=total)
        asm = budget.assemble(fixed_key='unicode', fixed_full=lambda: '身份与边界。' * 400,
            fixed_short=lambda: '身份与边界。', state_segs=[('🙂状态' * 120, 10)],
            retrieval_segs=[('𠮷🙂记忆' * 100, i) for i in range(8)],
            dialog=[{'role': 'user' if i % 2 == 0 else 'assistant', 'content': '混合文字abc🙂' * 20} for i in range(40)],
            user_text='𠮷🙂请认真回答' * 500)
        measured = (estimate_tokens(asm.system_prompt) + estimate_tokens(asm.prompt)
                    + sum(estimate_tokens(t['content']) + 16 for t in asm.contexts) + 128)
        assert measured == asm.usage.total and measured <= total


def test_social_reflection_is_bounded_and_event_driven(env):
    class Always:
        def random(self):
            return 0
    env.engine.rng = Always()
    social = env.engine.social
    social._learn('u', 'scope', '谢谢你介绍“新词语”', env.t, None)

    async def go():
        social.schedule_reflection('scope', env.t, 1, 0)
        first = social.reflection_task
        social.schedule_reflection('scope', env.t, 1, 0)
        assert social.reflection_task is first
        await env.engine.wait_background()
        assert env.engine.db.query_one('SELECT COUNT(*) n FROM reflections')['n'] == 1
    run(go())
