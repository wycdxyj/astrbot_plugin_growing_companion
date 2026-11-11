"""语音抽象层：STT（语音转文字）/ TTS（文字转语音），不写死任何服务商。

- STTProvider / TTSProvider：引擎与 main 只依赖这两个协议
- AstrBotSTT / AstrBotTTS：优先使用 AstrBot 后台已配置的 STT/TTS 供应商
  （context.get_using_stt_provider().get_text / get_using_tts_provider().get_audio）
- NullSTT / NullTTS：未配置时的空实现（返回 None，调用方自然退回文字）
- 想换成自己的服务：实现同名协议即可，在 main.py 里替换实例，不需要改引擎
"""
from __future__ import annotations

import logging
import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("growing_companion.speech")


class STTProvider(Protocol):
    async def transcribe(self, audio: str) -> str | None:
        """audio：本地路径或 URL。返回识别出的文字；失败返回 None。"""
        ...


class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> str | None:
        """返回生成好的音频文件路径（AstrBot 的 Record 组件当前只接受 wav）；失败返回 None。"""
        ...


class NullSTT:
    async def transcribe(self, audio: str) -> str | None:
        return None


class NullTTS:
    async def synthesize(self, text: str) -> str | None:
        return None


class AstrBotSTT:
    def __init__(self, context: Any):
        self.context = context

    async def transcribe(self, audio: str) -> str | None:
        try:
            prov = self.context.get_using_stt_provider()
            if prov is None:
                return None
            text = await prov.get_text(audio)
            return (text or "").strip() or None
        except Exception as e:
            log.warning("STT 失败：%s", e)
            return None


class AstrBotTTS:
    def __init__(self, context: Any, out_dir: str | Path | None = None):
        self.context = context
        self.out_dir = Path(out_dir) if out_dir else None   # 预留：需要自己落盘的适配器可用

    async def synthesize(self, text: str) -> str | None:
        try:
            prov = self.context.get_using_tts_provider()
            if prov is None or not text.strip():
                return None
            path = await prov.get_audio(text)
            return str(path) if path else None
        except Exception as e:
            log.warning("TTS 失败：%s", e)
            return None


class AudioFiles:
    """只清理本后端创建的过期缓存，留出平台读取文件的时间。"""
    def __init__(self, directory: Path):
        self.directory = directory

    def allocate(self, suffix: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in self.directory.glob('gc_*'):
            try:
                if path.is_file() and path.stat().st_mtime < time.time() - 86400:
                    path.unlink()
            except OSError:
                log.debug('语音缓存暂时无法清理')
        return self.directory / f'gc_{uuid.uuid4().hex}{suffix}'


class EdgeTTS:
    """现成音色 + 语速/音高；Edge TTS 不提供训练新音色的接口。"""
    def __init__(self, directory: Path, voice: str = 'zh-CN-XiaoxiaoNeural',
                 rate: str = '+0%', pitch: str = '+0Hz'):
        self.files, self.voice, self.rate, self.pitch = AudioFiles(directory), voice, rate, pitch
        self._limit = asyncio.Semaphore(2)

    async def synthesize(self, text: str) -> str | None:
        if not text.strip():
            return None
        async with self._limit:
            source = self.files.allocate('.mp3')
            target = source.with_suffix('.wav')
            process = None
            done = False
            try:
                import edge_tts  # 可选后端，Fish 用户无需加载它。
                communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch)
                await asyncio.wait_for(communicate.save(str(source)), timeout=60)
                options = {'creationflags': 0x08000000} if os.name == 'nt' else {}
                process = await asyncio.create_subprocess_exec(
                    'ffmpeg', '-nostdin', '-loglevel', 'error', '-i', str(source), '-ac', '1', '-ar', '24000',
                    '-y', str(target), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **options)
                await asyncio.wait_for(process.wait(), timeout=20)
                if process.returncode != 0 or not target.is_file() or target.stat().st_size <= 44:
                    raise RuntimeError('ffmpeg 转换失败')
                done = True
                return str(target.resolve())
            except Exception as exc:
                log.warning('Edge TTS 不可用（需 edge-tts 和 ffmpeg），退回文字：%s', type(exc).__name__)
                return None
            finally:
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
                source.unlink(missing_ok=True)
                if not done:
                    target.unlink(missing_ok=True)


class FishTTS:
    """语音只在云端合成，使用已克隆或音色库中可用的 reference_id。"""
    def __init__(self, directory: Path, voice_id: str, *, model: str = 's2.1-pro-free', speed: float = 1.0):
        self.files, self.voice_id, self.model, self.speed = AudioFiles(directory), voice_id, model, speed
        self._limit = asyncio.Semaphore(2)

    async def synthesize(self, text: str) -> str | None:
        return await self.synthesize_style(text, '平淡')

    async def synthesize_style(self, text: str, tone: str) -> str | None:
        from .cloud_voice import FishAudioClient
        if not text.strip():
            return None
        cues = {'亲昵': '[softly]', '赌气': '[reluctantly]', '兴奋': '[excited]', '敷衍': '[flatly]'}
        async with self._limit:
            try:
                client = FishAudioClient(os.getenv('FISH_API_KEY', ''), model=self.model)
                path = await client.synthesize(cues.get(tone, '') + text, self.voice_id,
                                               self.files.allocate('.wav'), speed=self.speed)
                return str(path)
            except Exception as exc:
                log.warning('云端语音不可用，退回文字：%s', type(exc).__name__)
                return None


def build_tts(context: Any, config: dict[str, Any], directory: Path) -> TTSProvider:
    backend = str(config.get('voice_backend', 'edge'))
    if backend == 'fish':
        return FishTTS(directory, str(config.get('fish_voice_id', '') or os.getenv('FISH_VOICE_ID', '')),
                       model=str(config.get('fish_tts_model', 's2.1-pro-free')),
                       speed=float(config.get('voice_speed', 1.0)))
    if backend == 'edge':
        return EdgeTTS(directory, str(config.get('edge_voice', 'zh-CN-XiaoxiaoNeural')),
                       str(config.get('edge_rate', '+0%')), str(config.get('edge_pitch', '+0Hz')))
    if backend == 'astrbot':
        return AstrBotTTS(context, directory)
    raise ValueError('voice_backend 只能为 edge、fish 或 astrbot')
