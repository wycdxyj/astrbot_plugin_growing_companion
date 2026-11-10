"""语音抽象层：STT（语音转文字）/ TTS（文字转语音），不写死任何服务商。

- STTProvider / TTSProvider：引擎与 main 只依赖这两个协议
- AstrBotSTT / AstrBotTTS：优先使用 AstrBot 后台已配置的 STT/TTS 供应商
  （context.get_using_stt_provider().get_text / get_using_tts_provider().get_audio）
- NullSTT / NullTTS：未配置时的空实现（返回 None，调用方自然退回文字）
- 想换成自己的服务：实现同名协议即可，在 main.py 里替换实例，不需要改引擎
"""
from __future__ import annotations

import logging
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
