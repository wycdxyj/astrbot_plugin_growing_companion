"""Fish Audio 云端克隆与合成；本地只做异步 HTTP 和文件 IO，无 GPU 依赖。"""
from __future__ import annotations

import asyncio
import mimetypes
import re
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import aiohttp


class VoiceAPIError(RuntimeError):
    """异常只包含状态和操作名，避免把密钥或录音内容写入日志。"""


class FishAudioClient:
    def __init__(self, api_key: str, *, model: str = "s2.1-pro-free",
                 base_url: str = "https://api.fish.audio", timeout: float = 90):
        if not api_key.strip():
            raise ValueError("请先设置 FISH_API_KEY 环境变量")
        # 未知名称会被服务端回退为另一个模型；拼写错误在本地直接报错。
        if model not in {"s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3"}:
            raise ValueError("不支持的 Fish TTS 模型名称")
        self.api_key, self.model = api_key, model
        self.base_url = base_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout, connect=15)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _check(response: aiohttp.ClientResponse, operation: str) -> None:
        if not 200 <= response.status < 300:
            raise VoiceAPIError(f"{operation}失败：HTTP {response.status}（401 密钥，402 额度，429 限流）")

    @staticmethod
    def _voice_id(voice_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice_id):
            raise ValueError("音色 ID 格式不正确")
        return voice_id

    async def create_voice(self, samples: list[Path], *, title: str = "小满的声音",
                           transcripts: list[str] | None = None) -> dict[str, Any]:
        """上传本人或已获授权的参考录音。创建一次即可，不要每轮聊天都创建。"""
        if not samples or (transcripts is not None and len(transcripts) != len(samples)):
            raise ValueError("至少一条录音；若提供文字，必须与录音逐条对应")
        for sample in samples:
            if not sample.is_file() or not 0 < sample.stat().st_size <= 32 * 1024 * 1024:
                raise ValueError(f"录音不存在、为空或超过本示例 32 MB 上限：{sample.name}")
        form = aiohttp.FormData()
        for key, value in {"type": "tts", "title": title, "visibility": "private",
                           "train_mode": "fast"}.items():
            form.add_field(key, value)
        # 不手写 multipart Content-Type：aiohttp 会添加正确的 boundary。
        with ExitStack() as stack:
            for sample in samples:
                stream = stack.enter_context(sample.open("rb"))
                form.add_field("voices", stream, filename=sample.name,
                               content_type=mimetypes.guess_type(sample.name)[0] or "application/octet-stream")
            for transcript in transcripts or []:
                form.add_field("texts", transcript)
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(f"{self.base_url}/model", data=form,
                                        headers=self._headers(), allow_redirects=False) as response:
                    self._check(response, "创建音色")
                    result = await response.json()
        if not isinstance(result, dict) or not result.get("_id"):
            raise VoiceAPIError("创建音色返回缺少 _id")
        self._voice_id(str(result["_id"]))
        return result

    async def wait_ready(self, voice_id: str, *, deadline_seconds: float = 180) -> None:
        """有界轮询，不把创建中的音色当成训练完成；超时保留 ID，下次可继续。"""
        voice_id = self._voice_id(voice_id)
        deadline = time.monotonic() + deadline_seconds
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                async with session.get(f"{self.base_url}/model/{voice_id}", headers=self._headers(),
                                       timeout=aiohttp.ClientTimeout(total=max(0.01, remaining)),
                                       allow_redirects=False) as response:
                    self._check(response, "查询音色")
                    info = await response.json()
                state = info.get("state") if isinstance(info, dict) else None
                if state == "trained":
                    return
                if state in {"failed", "error"}:
                    raise VoiceAPIError("云端音色创建失败，请检查参考录音")
                if state not in {"created", "training", "pending"}:
                    raise VoiceAPIError(f"未知音色状态：{state!r}")
                await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
        raise TimeoutError(f"音色尚未就绪；保留 ID {voice_id}，稍后查询，勿重复创建")

    async def synthesize(self, text: str, voice_id: str, output: Path, *, speed: float = 1.0) -> Path:
        """流式落盘 WAV，不把整段音频攒在内存；失败不留下伪音频或覆盖旧文件。"""
        voice_id = self._voice_id(voice_id)
        if not text.strip() or len(text) > 2000:
            raise ValueError("本示例每次合成 1–2000 字")
        if not 0.7 <= speed <= 1.3:
            raise ValueError("本插件语速限定为 0.7–1.3")
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + f".{uuid.uuid4().hex}.part")
        payload = {"text": text, "reference_id": voice_id, "format": "wav",
                   "sample_rate": 24000, "prosody": {"speed": speed, "volume": 0}}
        headers = {**self._headers(), "model": self.model}
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(f"{self.base_url}/v1/tts", json=payload, headers=headers,
                                        allow_redirects=False) as response:
                    self._check(response, "语音合成")
                    size = 0
                    with temporary.open("wb") as target:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > 64 * 1024 * 1024:
                                raise VoiceAPIError("返回音频超过本示例的 64 MB 上限")
                            # 有界小块落盘；取消时没有仍在后台写入的线程占用临时文件。
                            target.write(chunk)
            with temporary.open("rb") as source:
                header = source.read(12)
            if size <= 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                raise VoiceAPIError("服务没有返回有效的 RIFF/WAVE 音频")
            temporary.replace(output)
            return output
        finally:
            temporary.unlink(missing_ok=True)
