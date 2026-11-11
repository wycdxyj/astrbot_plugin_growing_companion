"""运行 python examples/cloud_voice_demo.py --help；无需 AstrBot、CUDA 或模型权重。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.cloud_voice import FishAudioClient  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="云端克隆一次、保存音色 ID、反复生成 WAV")
    parser.add_argument("--sample", type=Path, action="append", help="参考录音；可重复传入")
    parser.add_argument("--transcript", action="append", help="与 sample 一一对应的原文，可省略")
    parser.add_argument("--voice-id", default=os.getenv("FISH_VOICE_ID", ""), help="自己的或可用的云端音色 ID")
    parser.add_argument("--voice-file", type=Path, default=Path("voice.json"))
    parser.add_argument("--text", default="爸爸，刚才那个故事，我还想听后面。")
    parser.add_argument("--output", type=Path, default=Path("daughter.wav"))
    args = parser.parse_args()
    client = FishAudioClient(os.getenv("FISH_API_KEY", ""), model=os.getenv("FISH_TTS_MODEL", "s2.1-pro-free"))
    voice_id = args.voice_id
    if args.sample:
        if voice_id or args.voice_file.exists():
            parser.error("创建新音色时请指定一个尚不存在的 --voice-file，且不要传 --voice-id")
        # exclusive create 防止两次启动重复创建；失败保留文件，不自动重试付费 POST。
        args.voice_file.parent.mkdir(parents=True, exist_ok=True)
        with args.voice_file.open("x", encoding="utf-8") as saved:
            saved.write('{"status":"creating"}\n')
        model = await client.create_voice(args.sample, transcripts=args.transcript)
        voice_id = model["_id"]
        args.voice_file.write_text(json.dumps({"voice_id": voice_id, "state": model.get("state")},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存音色 ID：{voice_id}")
        await client.wait_ready(voice_id)
    elif not voice_id and args.voice_file.exists():
        voice_id = json.loads(args.voice_file.read_text(encoding="utf-8")).get("voice_id", "")
        if voice_id:
            await client.wait_ready(voice_id)
    if not voice_id:
        parser.error("没有录音也没有音色 ID：先从 Fish 音色库选一个可用 ID；克隆时再提供 --sample")
    path = await client.synthesize(args.text, voice_id, args.output)
    print(f"音频已保存：{path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (OSError, RuntimeError, ValueError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
