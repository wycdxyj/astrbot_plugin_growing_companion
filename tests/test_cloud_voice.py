"""本地 HTTP 契约测试；不使用真实密钥、不调用付费服务。"""
import asyncio
import io
import wave
from pathlib import Path

import pytest
from aiohttp import web

from growing_companion.core.cloud_voice import FishAudioClient, VoiceAPIError
from growing_companion.core.expression import ReplySender
from growing_companion.core.models import BehaviorPlan, ContextUsage, EngineReply

from .conftest import run


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\x00\x00' * 1200)
    return output.getvalue()


def test_clone_once_then_generate_wav(tmp_path):
    calls = []
    async def create(request):
        assert request.headers['Authorization'] == 'Bearer test-key'
        reader, fields = await request.multipart(), {}
        while part := await reader.next():
            fields[part.name] = bytes(await part.read())
        assert fields['visibility'] == b'private' and fields['train_mode'] == b'fast'
        assert fields['voices'] == wav_bytes() and fields['texts'].decode() == '这是参考录音。'
        calls.append('clone')
        return web.json_response({'_id': 'voice-test', 'state': 'created'}, status=201)
    async def state(request): return web.json_response({'_id': 'voice-test', 'state': 'trained'})
    async def tts(request):
        body = await request.json()
        assert body['reference_id'] == 'voice-test' and body['format'] == 'wav'
        assert body['text'] == '爸爸，看这里。' and body['sample_rate'] == 24000
        assert request.headers['model'] == 's2.1-pro-free'
        calls.append('tts')
        return web.Response(body=wav_bytes(), content_type='audio/wav')
    async def go():
        app = web.Application()
        app.router.add_post('/model', create)
        app.router.add_get('/model/{id}', state)
        app.router.add_post('/v1/tts', tts)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            sample = tmp_path / 'sample.wav'
            sample.write_bytes(wav_bytes())
            client = FishAudioClient('test-key', base_url=f'http://127.0.0.1:{port}')
            model = await client.create_voice([sample], transcripts=['这是参考录音。'])
            await client.wait_ready(model['_id'])
            for i in range(2):
                path = await client.synthesize('爸爸，看这里。', model['_id'], tmp_path / f'out{i}.wav')
                assert path.read_bytes() == wav_bytes()
        finally:
            await runner.cleanup()
    run(go())
    assert calls == ['clone', 'tts', 'tts']


@pytest.mark.parametrize('status,body', [(401, b'unauthorized'), (429, b'limited'), (200, b'{"error":"not audio"}')])
def test_http_errors_and_bad_audio_do_not_overwrite_output(tmp_path, status, body):
    async def handler(request): return web.Response(status=status, body=body)
    async def go():
        app = web.Application()
        app.router.add_post('/v1/tts', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        output = tmp_path / 'keep.wav'
        output.write_bytes(b'old-file')
        try:
            client = FishAudioClient('test-key', base_url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}')
            with pytest.raises(VoiceAPIError):
                await client.synthesize('你好', 'voice-test', output)
            assert output.read_bytes() == b'old-file' and not list(tmp_path.glob('*.part'))
        finally:
            await runner.cleanup()
    run(go())


def test_cloud_not_ready_fails_bounded_and_preserves_id():
    async def handler(request): return web.json_response({'state': 'failed'})
    async def go():
        app = web.Application()
        app.router.add_get('/model/{id}', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        try:
            client = FishAudioClient('test-key', base_url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}')
            with pytest.raises(VoiceAPIError): await client.wait_ready('voice-test')
            with pytest.raises(TimeoutError): await client.wait_ready('voice-test', deadline_seconds=0)
        finally:
            await runner.cleanup()
    run(go())


def test_voice_failure_falls_back_to_text_once():
    class BrokenTTS:
        async def synthesize(self, text): raise TimeoutError('cloud')
    received, delivered = [], []
    async def send(text): received.append(text)
    async def wrong(path): raise AssertionError('没有生成音频不应发送 Record')
    reply = EngineReply('嗯。', BehaviorPlan(), ContextUsage(), prefer_voice=True, parts=['嗯。'])
    run(ReplySender().send(reply, send_text=send, send_voice=wrong, delivered=delivered,
                           tts=BrokenTTS(), voice_enabled=True))
    assert received == delivered == ['嗯。']


def test_no_samples_no_key_or_unknown_model_fail_locally(tmp_path):
    with pytest.raises(ValueError): FishAudioClient('')
    with pytest.raises(ValueError): FishAudioClient('key', model='typo')
    with pytest.raises(ValueError): run(FishAudioClient('key').create_voice([]))
    with pytest.raises(ValueError): run(FishAudioClient('key').create_voice([Path('missing.wav')]))


def test_edge_generates_mp3_then_converts_to_wav(tmp_path, monkeypatch):
    import sys
    import types
    from growing_companion.core.speech import EdgeTTS
    calls = []
    class Communicate:
        def __init__(self, text, voice, *, rate, pitch):
            calls.append((text, voice, rate, pitch))
        async def save(self, path): Path(path).write_bytes(b'mock-mp3')
    class Process:
        returncode = 0
        async def wait(self): return 0
    async def ffmpeg(*args, **kwargs):
        assert args[0] == 'ffmpeg' and '-nostdin' in args
        Path(args[-1]).write_bytes(wav_bytes())
        return Process()
    monkeypatch.setitem(sys.modules, 'edge_tts', types.SimpleNamespace(Communicate=Communicate))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', ffmpeg)
    path = run(EdgeTTS(tmp_path, rate='-5%', pitch='+1Hz').synthesize('嗯，知道啦。'))
    assert Path(path).read_bytes() == wav_bytes() and not list(tmp_path.glob('*.mp3'))
    assert calls == [('嗯，知道啦。', 'zh-CN-XiaoxiaoNeural', '-5%', '+1Hz')]
