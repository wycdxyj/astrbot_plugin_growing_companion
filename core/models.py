"""核心数据模型：纯数据结构与通用小工具，不依赖 AstrBot，也不做 IO。

设计原则：所有“她”的内部状态都用连续数值表达（0~1），
而不是布尔开关或固定规则——行为由状态驱动，而不是由配置项驱动。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# 基础枚举与常量
# --------------------------------------------------------------------------- #

EMOTIONS: tuple[str, ...] = ("joy", "trust", "fear", "sadness", "curiosity", "anger")
EMOTION_CN: dict[str, str] = {
    "joy": "开心", "trust": "信任", "fear": "害怕",
    "sadness": "难过", "curiosity": "好奇", "anger": "生气",
}


class Stage(str, Enum):
    """成长阶段。阶段只是“能力池”的边界，不是自动开关。"""
    INFANT = "infant"
    CHILD = "child"
    TEEN = "teen"
    YOUNG_ADULT = "young_adult"


STAGES: list[Stage] = [Stage.INFANT, Stage.CHILD, Stage.TEEN, Stage.YOUNG_ADULT]
STAGE_CN: dict[str, str] = {
    "infant": "婴幼儿期", "child": "儿童期", "teen": "少年期", "young_adult": "青年期",
}


def stage_index(stage: "Stage | str") -> int:
    return STAGES.index(Stage(stage))


class MemoryType(str, Enum):
    """分层记忆。working 仅指工作记忆（滑动窗口），不落 memories 表。"""
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    EMOTIONAL = "emotional"
    RELATIONAL = "relational"


MEMORY_TYPE_CN: dict[str, str] = {
    "working": "工作", "episodic": "情景", "semantic": "语义",
    "emotional": "情绪", "relational": "关系",
}

# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------- #
# 感知 / 情绪 / 身体 / 注意力
# --------------------------------------------------------------------------- #


@dataclass
class Percept:
    """感知器输出：一次输入事件（消息）经规则提取后的结构化感知。"""
    text: str
    user_id: str
    ts: float
    is_voice: bool = False
    cues: set[str] = field(default_factory=set)       # praise/scold/comfort/apology/question/joke/...
    keywords: list[str] = field(default_factory=list)  # 粗粒度关键词（二元组）
    gap_s: float | None = None                         # 距上次对话的秒数


@dataclass
class EmotionSnapshot:
    """某一时刻的情绪快照（含“嘴硬心软”“事后后悔”等矛盾心理标记）。"""
    values: dict[str, float]
    dominant: str = "trust"
    secondary: str | None = None
    intensity: float = 0.0
    valence: float = 0.0
    arousal: float = 0.0
    mixed: bool = False       # 多种强情绪并存
    tsundere: bool = False    # 嘴硬心软
    regret: float = 0.0       # 事后后悔强度

    def active(self, thr: float = 0.25) -> dict[str, float]:
        return {k: v for k, v in self.values.items() if v >= thr}


@dataclass
class BodyState:
    """“身体感”：不是真生理，是状态驱动表达的内部变量。"""
    energy: float = 0.7
    sleepiness: float = 0.2
    phase: str = "awake"        # awake / drowsy / asleep / groggy
    groggy_until: float = 0.0   # 刚睡醒的迷糊期
    woken_by_user: bool = False
    updated_at: float = 0.0


@dataclass
class AttentionState:
    """注意力与“不在场感”：她不是永远待命。"""
    focus: float = 0.8
    away_until: float = 0.0
    away_reason: str = ""
    social_need: float = 0.4     # 想被关注、想亲近
    solitude_need: float = 0.2   # 想独处
    updated_at: float = 0.0


@dataclass
class TimeSense:
    now: float
    hour: int
    weekday_cn: str
    period: str            # 深夜/凌晨/早晨/上午/中午/下午/傍晚/晚上
    is_weekend: bool
    since_last_contact_s: float | None
    absence_label: str     # 刚刚/一会儿/半天/一天多/好几天/很久...


@dataclass
class Relationship:
    person_id: str
    role: str = "father"
    trust: float = 0.5
    attachment: float = 0.5
    security: float = 0.5
    longing: float = 0.0
    last_contact_at: float = 0.0
    shared_memory_count: int = 0
    conflict_open: bool = False
    last_change_note: str = ""
    first_met_at: float = 0.0


# --------------------------------------------------------------------------- #
# 认知 / 成长 / 规划
# --------------------------------------------------------------------------- #


@dataclass
class Appraisal:
    """认知引擎对一次输入的判断（规则版，留有扩展接口）。"""
    intent: str = "chat"                 # chat/question/praise/scold/share/request/comfort/farewell/greeting
    topics: list[str] = field(default_factory=list)
    novelty: float = 0.0                 # 对她来说多新鲜
    unknown_topic: bool = False          # 元认知：我不懂
    value_conflict: str | None = None    # 与她的价值观冲突
    guess_father_state: str | None = None  # 心智理论：对父亲状态的猜测
    misread: bool = False                # 允许误会
    isolation_signal: bool = False       # 用户有疏远真人关系的倾向
    safety_flag: str | None = None       # crisis / medical / danger
    sincere_identity_question: bool = False  # 认真跳出角色问“你是不是AI/真人”


@dataclass
class GrowthProfile:
    """成长引擎输出给规划器/提示词的“她现在能做到什么”。"""
    stage: str
    language_tier: int                   # 0咿呀 1蹦词 2短句 3完整句 4复杂表达
    language_label: str
    max_chars: int
    stretch_prob: float                  # 在“还在学”的能力上尝试更高表达的概率
    cognition_level: float
    cognitive_style: str
    performance: dict[str, float] = field(default_factory=dict)  # 各能力当前“表现”（受状态影响）


@dataclass
class BehaviorPlan:
    mode: str = "normal"                 # normal/sleepy/sulk/away/clingy/regressed/safety/stranger/cold
    max_chars: int = 80
    language_tier: int = 3
    directives: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # 内部原因（给查看面板/调试，不给用户）
    silent: bool = False
    reply_delay_s: float = 0.0
    safety: bool = False
    ask_curiosity: str | None = None
    surfaced_memory_id: int | None = None
    share_reflection_id: int | None = None
    share_dream_id: int | None = None
    stretch_attempt: bool = False
    prefer_voice: bool = False


@dataclass
class Recalled:
    memory: dict[str, Any]
    score: float
    via: str = "direct"        # direct / assoc / awakened / spontaneous
    vague: bool = False        # 元记忆：记不清
    text: str = ""


@dataclass
class ContextUsage:
    fixed: int = 0
    state: int = 0
    retrieval: int = 0
    dialog: int = 0
    total: int = 0
    budget: int = 0
    fixed_cache_hit: bool = False
    dropped_memories: int = 0
    trimmed_turns: int = 0


@dataclass
class AssembledContext:
    system_prompt: str
    contexts: list[dict[str, str]]
    prompt: str
    usage: ContextUsage


@dataclass
class EngineReply:
    text: str
    plan: BehaviorPlan
    usage: ContextUsage
    silent: bool = False
    delay_s: float = 0.0
    prefer_voice: bool = False
    events: list[str] = field(default_factory=list)  # 里程碑等事件描述（调试用）
    speech_token: str = ''  # 持久化发言租约；发送成功后才确认。
    scope_id: str = ''


@dataclass(frozen=True)
class SocialDecision:
    respond: bool
    probability: float
    reason: str
