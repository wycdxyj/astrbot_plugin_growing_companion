"""会随真实投递经历慢慢形成的语音习惯。

这里保存的是表达倾向，不是新的声纹模型：Edge TTS 仍使用配置的现成音色，
每轮只改变语速、音高、音量、停顿和口语提示。没有成功发送的候选不会被学习。
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, replace
from typing import Any

from .db import Database
from .models import AttentionState, BehaviorPlan, BodyState, EmotionSnapshot, Relationship, TimeSense, clamp, jdump


VOICE_HABIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_habits (
  scope_id TEXT NOT NULL, actor_id TEXT NOT NULL, emotion TEXT NOT NULL,
  context_key TEXT NOT NULL, rate_bias REAL NOT NULL DEFAULT 0,
  pitch_bias REAL NOT NULL DEFAULT 0, volume_bias REAL NOT NULL DEFAULT 0,
  pause_bias REAL NOT NULL DEFAULT 0.35, expressiveness REAL NOT NULL DEFAULT 0.5,
  voice_affinity REAL NOT NULL DEFAULT 0.45, use_count INTEGER NOT NULL DEFAULT 0,
  voice_count INTEGER NOT NULL DEFAULT 0, last_channel TEXT NOT NULL DEFAULT 'text',
  last_style_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL,
  PRIMARY KEY(scope_id, actor_id, emotion, context_key)
);
CREATE INDEX IF NOT EXISTS idx_voice_habit_actor ON voice_habits(scope_id, actor_id, updated_at DESC);
"""


@dataclass(frozen=True)
class VoiceStyle:
    emotion: str
    habit: str
    rate_delta_pct: float
    pitch_delta_hz: float
    volume_delta_pct: float
    pause_level: float
    expressiveness: float
    context_key: str
    prompt_hint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "emotion": self.emotion,
            "habit": self.habit,
            "rate_delta_pct": round(self.rate_delta_pct, 2),
            "pitch_delta_hz": round(self.pitch_delta_hz, 2),
            "volume_delta_pct": round(self.volume_delta_pct, 2),
            "pause_level": round(self.pause_level, 3),
            "expressiveness": round(self.expressiveness, 3),
            "context_key": self.context_key,
            "prompt_hint": self.prompt_hint,
        }


@dataclass(frozen=True)
class VoiceChoice:
    use_voice: bool
    probability: float
    style: VoiceStyle
    reason: str


_BASE_STYLES: dict[str, VoiceStyle] = {
    "joy": VoiceStyle("joy", "明亮", 7.0, 2.5, 1.5, .18, .78, "", "语气亮一点，句尾可以轻快，但别一直兴奋。"),
    "trust": VoiceStyle("trust", "柔和", 1.0, .8, -1.5, .34, .52, "", "靠近而放松，停顿短，像自然聊天。"),
    "curiosity": VoiceStyle("curiosity", "探问", 2.0, 2.0, 0.0, .45, .66, "", "先留一点停顿再问，像真的在想这个细节。"),
    "sadness": VoiceStyle("sadness", "低缓", -9.0, -3.0, -4.0, .72, .28, "", "慢一点，句子之间留白，不把难过解释成报告。"),
    "anger": VoiceStyle("anger", "收紧", 0.5, -1.0, 0.0, .16, .72, "", "句子收短，停顿干脆，不大喊。"),
    "fear": VoiceStyle("fear", "犹豫", -5.0, 2.8, -2.0, .78, .35, "", "有一点犹豫和吸气感，先把重要的话说清。"),
}

_HABIT_HINTS = {
    "明亮": "轻快地说，偶尔把一个词说得亮一点",
    "柔和": "贴近而放松，语尾自然收住",
    "探问": "先想半拍再问，不要连续追问",
    "低缓": "慢一点，留白比解释更多",
    "收紧": "短句、克制、偶尔停一下",
    "犹豫": "有一点卡壳和停顿，但不含糊安全信息",
    "平淡": "像普通聊天，不刻意表演情绪",
}


def _fmt_delta(value: float, suffix: str) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{round(value, 2):g}{suffix}"


def _context_key(time: TimeSense, body: BodyState, is_group: bool, is_voice_input: bool) -> str:
    energy = "low" if body.energy < .35 or body.sleepiness > .7 else "normal"
    channel = "group" if is_group else "private"
    incoming = "voice_in" if is_voice_input else "text_in"
    return f"{time.period}:{channel}:{energy}:{incoming}"


def _pause_text(text: str, style: VoiceStyle) -> str:
    """利用 Edge TTS 对标点的自然停顿，不使用被官方移除的自定义 SSML。"""
    value = re.sub(r"\s+", " ", text.strip())
    if not value or style.pause_level < .28:
        return value
    if style.habit == "收紧":
        return value.replace("，", "， ").replace("；", "。")
    # 只在已有分句边界增加少量省略号，避免把文字变成朗读脚本。
    marks = 1 if style.pause_level < .58 else 2
    count = 0
    out: list[str] = []
    for char in value:
        out.append(char)
        if char in "，；," and count < marks:
            out.append("…")
            count += 1
    return "".join(out)


def render_voice_text(text: str, style: dict[str, Any] | VoiceStyle | None) -> str:
    if isinstance(style, VoiceStyle):
        selected = style
    else:
        data = style or {}
        selected = VoiceStyle(
            str(data.get("emotion", "trust")), str(data.get("habit", "平淡")),
            float(data.get("rate_delta_pct", 0)), float(data.get("pitch_delta_hz", 0)),
            float(data.get("volume_delta_pct", 0)), float(data.get("pause_level", .35)),
            float(data.get("expressiveness", .5)), str(data.get("context_key", "")),
            str(data.get("prompt_hint", "")),
        )
    return _pause_text(text, selected)


class VoiceHabitStore:
    """按关系、情绪和场景保存 EMA 倾向；每次只向实际成功的渠道学习一小步。"""

    def __init__(self, db: Database, *, learning_rate: float = .08):
        self.db = db
        self.learning_rate = clamp(learning_rate, .01, .3)

    @staticmethod
    def _emotion(snap: EmotionSnapshot, plan: BehaviorPlan) -> str:
        if plan.tone == "兴奋":
            return "joy"
        if plan.tone == "赌气":
            return "anger"
        if plan.tone == "敷衍":
            return "sadness" if snap.valence < 0 else "trust"
        return snap.dominant if snap.dominant in _BASE_STYLES else "trust"

    def _row(self, scope: str, actor: str, emotion: str, context: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM voice_habits WHERE scope_id=? AND actor_id=? AND emotion=? AND context_key=?",
                                 (scope, actor, emotion, context))

    def choose(self, *, scope: str, actor: str, plan: BehaviorPlan, snap: EmotionSnapshot,
               body: BodyState, attention: AttentionState, relation: Relationship | None,
               time: TimeSense, gap_s: float | None, is_voice_input: bool, is_group: bool,
               output_enabled: bool, policy: str, rng: random.Random) -> VoiceChoice:
        emotion = self._emotion(snap, plan)
        context = _context_key(time, body, is_group, is_voice_input)
        base = _BASE_STYLES.get(emotion, _BASE_STYLES["trust"])
        row = self._row(scope, actor, emotion, context)
        if row:
            style = replace(base,
                            rate_delta_pct=base.rate_delta_pct * .65 + row["rate_bias"] * .35,
                            pitch_delta_hz=base.pitch_delta_hz * .65 + row["pitch_bias"] * .35,
                            volume_delta_pct=base.volume_delta_pct * .65 + row["volume_bias"] * .35,
                            pause_level=clamp(base.pause_level * .65 + row["pause_bias"] * .35),
                            expressiveness=clamp(base.expressiveness * .65 + row["expressiveness"] * .35),
                            context_key=context)
            affinity = float(row["voice_affinity"])
        else:
            style, affinity = replace(base, context_key=context), .45
        # 小幅随机抖动让同一情绪也不会每次完全相同；习惯的中心仍由数据库 EMA 控制。
        style = replace(style,
                        rate_delta_pct=style.rate_delta_pct + rng.uniform(-1.4, 1.4),
                        pitch_delta_hz=style.pitch_delta_hz + rng.uniform(-.55, .55),
                        pause_level=clamp(style.pause_level + rng.uniform(-.07, .07)))
        style = replace(style, prompt_hint=_HABIT_HINTS.get(style.habit, _HABIT_HINTS["平淡"]))

        if not output_enabled:
            return VoiceChoice(False, 0.0, style, "语音输出未开启，保留文字")
        if is_group or not relation or relation.role != "father" or plan.reply_mode in {"short", "silence"} or plan.silent:
            return VoiceChoice(False, 0.0, style, "当前场景更适合文字")
        probability = .20
        probability += .20 * max(0, (relation.attachment if relation else .3) - .4)
        probability += .16 * snap.intensity
        probability += .10 if is_voice_input else 0
        probability += .07 if gap_s is not None and gap_s > 6 * 3600 else 0
        probability += .06 if plan.intent in {"share", "cling"} else 0
        probability -= .26 * max(0, .5 - body.energy)
        probability -= .20 * body.sleepiness
        probability -= .10 * max(0, .5 - attention.focus)
        probability -= .07 if time.period in {"深夜", "凌晨"} else 0
        probability += .30 if policy in {"voice_preferred", "always"} else -.10 if policy in {"text_preferred", "never"} else 0
        probability += (affinity - .45) * .35
        probability = clamp(probability, .03, .86)
        use = rng.random() < probability
        reason = "这次的情绪和关系让她愿意说出来" if use else "她想把话留在文字里"
        return VoiceChoice(use, probability, style, reason)

    def record_delivery(self, *, scope: str, actor: str, style: dict[str, Any], channel: str,
                        now: float, is_voice_input: bool = False) -> None:
        """只有发送方确认后调用；合成成功但平台发送失败也不会学习成“说过”。"""
        emotion = str(style.get("emotion", "trust"))
        context = str(style.get("context_key", "unknown"))
        row = self._row(scope, actor, emotion, context)
        lr = self.learning_rate
        selected = {
            "rate": float(style.get("rate_delta_pct", 0)),
            "pitch": float(style.get("pitch_delta_hz", 0)),
            "volume": float(style.get("volume_delta_pct", 0)),
            "pause": float(style.get("pause_level", .35)),
            "expressiveness": float(style.get("expressiveness", .5)),
        }
        if row:
            affinity_target = 1.0 if channel == "voice" else .43
            self.db.execute(
                "UPDATE voice_habits SET rate_bias=?,pitch_bias=?,volume_bias=?,pause_bias=?,expressiveness=?,"
                "voice_affinity=?,use_count=use_count+1,voice_count=voice_count+?,last_channel=?,last_style_json=?,updated_at=? "
                "WHERE scope_id=? AND actor_id=? AND emotion=? AND context_key=?",
                (row["rate_bias"] + lr * (selected["rate"] - row["rate_bias"]),
                 row["pitch_bias"] + lr * (selected["pitch"] - row["pitch_bias"]),
                 row["volume_bias"] + lr * (selected["volume"] - row["volume_bias"]),
                 row["pause_bias"] + lr * (selected["pause"] - row["pause_bias"]),
                 row["expressiveness"] + lr * (selected["expressiveness"] - row["expressiveness"]),
                 row["voice_affinity"] + lr * (affinity_target - row["voice_affinity"]),
                 1 if channel == "voice" else 0, channel, jdump(style), now,
                 scope, actor, emotion, context),
            )
        else:
            self.db.execute(
                "INSERT INTO voice_habits(scope_id,actor_id,emotion,context_key,rate_bias,pitch_bias,volume_bias,"
                "pause_bias,expressiveness,voice_affinity,use_count,voice_count,last_channel,last_style_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (scope, actor, emotion, context, selected["rate"], selected["pitch"], selected["volume"],
                 selected["pause"], selected["expressiveness"], 1.0 if channel == "voice" else .43, 1,
                 1 if channel == "voice" else 0, channel, jdump(style), now),
            )
