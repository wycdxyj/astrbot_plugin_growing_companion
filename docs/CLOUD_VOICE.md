# 4 核 8 GB、无 GPU：云端克隆与语音生成

本方案使用 Fish Audio 的托管接口：录音上传后得到音色 ID，文本发送到云端，下载 WAV，再交给 AstrBot 的 `Record` 消息组件。本地无需部署 GPT-SoVITS 或其他语音模型。

没有任何参考录音时，可以先使用 Fish 音色库中账号可用的音色 ID。克隆特定声音仍需要本人或获授权的参考录音；“继续聊天”不会让云端音色自动训练。Edge TTS 只提供现成音色和语速/音高调整，不能训练独有音色。

## 直接运行 Python 示例

从插件根目录运行。独立示例只需要 `aiohttp`，不需要安装 AstrBot：

```bash
python -m pip install 'aiohttp>=3.11.18,<4'
export FISH_API_KEY='你的 API Key'
export FISH_TTS_MODEL='s2.1-pro-free'

# 当前没有录音：先在 Fish 音色库选一个账号可使用的 ID。
python examples/cloud_voice_demo.py --voice-id '可用的音色ID' --text '爸爸，刚才的故事还有后面吗？' --output daughter.wav
```

Windows PowerShell 设置环境变量使用 `$env:FISH_API_KEY='你的 API Key'`；其余 Python 命令相同。
`s2.1-pro-free` 是核对文档时列出的开发模型，能否调用取决于账号及服务端当前规则。程序不会在失败时自动切换其他计费模型。可通过 `FISH_TTS_MODEL` 明确选择 `s2.1-pro`、`s2-pro` 或 `drama-3`。

之后有录音时，创建一次私有音色：

```bash
python examples/cloud_voice_demo.py --sample reference.wav --transcript '这段录音里实际说出的原文。' --voice-file voice-v1.json

# 后续复用本地保存的 ID，不再重复上传或创建音色。
python examples/cloud_voice_demo.py --voice-file voice-v1.json --text '嗯，我刚才在想那只猫。' --output reply.wav
```

`reference.wav` 是你准备的文件，不随插件附送。转写文字是录音实际内容，不是这次想生成的话。多条录音可以重复传入 `--sample`，提供转写时 `--transcript` 数量必须一致。

示例调用的核心接口：

```python
import asyncio
import os
from pathlib import Path
from core.cloud_voice import FishAudioClient

async def demo() -> None:
    client = FishAudioClient(os.environ['FISH_API_KEY'])
    # 无录音时，直接使用音色库中可用的 FISH_VOICE_ID。
    voice_id = os.environ['FISH_VOICE_ID']
    output = await client.synthesize('爸爸，我想把刚才的故事听完。', voice_id, Path('reply.wav'))
    print(output)

asyncio.run(demo())
```

完整创建、状态轮询、ID 保存和复用代码在 [examples/cloud_voice_demo.py](../examples/cloud_voice_demo.py)，异步 HTTP 实现在 [core/cloud_voice.py](../core/cloud_voice.py)。

## 接入 AstrBot

向 **运行 AstrBot 的进程** 提供 `FISH_API_KEY`；Docker 可用环境变量注入，systemd 可通过服务环境配置提供。只在交互终端设置变量不会影响已经运行的 AstrBot，需要重启对应进程。

在插件配置中设置：

```json
{
  "voice_output_enabled": true,
  "voice_backend": "fish",
  "fish_voice_id": "云端返回的_id或可用音色ID",
  "fish_tts_model": "s2.1-pro-free",
  "voice_reply_policy": "natural",
  "voice_speed": 1.0
}
```

`natural` 随行为规划决定是否用语音；首次试听可暂设 `always`。沉默不会为了发声而被改成回复；群聊也不会因为该配置而发送语音。首次角色仍处于 infant 时表达能力很低，这是成长限制，不是 TTS 故障。

Fish 云端后端按规划语气添加少量 S2 风格提示，例如 `[softly]` 或 `[excited]`。它们只提交给 TTS，不显示在聊天或写成助手说过的词。表现效果需要实际试听，不承诺不同模型完全一致。

AstrBot 发送 WAV：

```python
import astrbot.api.message_components as Comp

path = await self.tts.synthesize('嗯，我在听。')
if path:
    await event.send(event.chain_result([Comp.Record(file=path, url=path)]))
else:
    await event.send(event.plain_result('嗯，我在听。'))
```

实际插件由 `ReplySender` 负责延迟、分条和失败回执；无需再添加另一套监听函数。

## 长期改善音色的做法

说话风格（语速、停顿、用词、亲昵程度）与音色克隆分开。当前代码持续保存行为表达经历，语音参数保存在插件配置；它没有把普通聊天或合成音频自动送去训练。

有更清晰的真实录音后，可把保留的代表性样本与新样本一起创建一个新音色版本，保存为 `voice-v2.json`。试听后把 `fish_voice_id` 切换到新 ID，旧 ID 保留便于回退。**本示例调用的是云端快速克隆接口，没有实现模型权重的持续微调，也不声称支持增量训练。**

为避免重复创建，示例在提交前建立 `voice-*.json` 占位文件。如果请求超时且文件只有 `creating`，先在云端控制台核对是否已经创建成功，再填入正确的 `voice_id`；不要自动重发创建请求。服务已返回 ID 时，轮询超时也会保留该 ID。

## 已核对的接口与验证范围

- `POST https://api.fish.audio/model`：`multipart/form-data` 上传 `voices` 文件，`type=tts`、`train_mode=fast`、`visibility=private`；响应读取 `_id`。不要把本地路径当 JSON 字符串上传。见 [官方克隆教程](https://docs.fish.audio/features/voice-cloning) 和 [创建接口源文档](https://github.com/fishaudio/docs/blob/main/api-reference/endpoint/model/create-model.mdx)。
- `GET /model/{id}`：确认 `state=trained`，`created`/`training` 有界等待，`failed` 报错。
- `POST https://api.fish.audio/v1/tts`：JSON 传 `text`、`reference_id`、`format=wav`、`sample_rate=24000`，模型名放 `model` HTTP 头。见 [官方 TTS 接口](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech)。
- [AstrBot 发送文档](https://docs.astrbot.app/dev/star/guides/send-message.html) 要求语音用 WAV，因此 Edge MP3 通过 ffmpeg 转码，Fish 直接请求 WAV。[Edge TTS](https://github.com/rany2/edge-tts) 使用微软在线服务。

核对日期：2026-11-11。HTTP 模拟测试覆盖文件上传、复用 ID、合成参数、WAV 内容、401/429、错误响应、失败状态及超时边界；没有真实 API Key/录音，未实际提交克隆或验证音质。
