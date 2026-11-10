"""感知器：把消息 + 时间 + 事件转成结构化 Percept（规则版，轻量，不占大模型上下文）。"""
from __future__ import annotations

import re

from .models import Percept

# 线索词表（可随成长/复盘扩展；规则只负责“感知到什么”，不决定“怎么反应”）
CUE_PATTERNS: dict[str, str] = {
    "praise": r"乖|真棒|好棒|棒棒|聪明|做得好|喜欢你|爱你|骄傲|厉害|真好|宝贝",
    "scold": r"笨|不听话|烦死|闭嘴|不许|别吵|讨厌你|凶你|不乖|再这样|滚|吵死",
    "comfort": r"抱抱|没事|别怕|不哭|不难过|有我在|摸摸头|乖乖",
    "apology": r"对不起|我错了|抱歉|不好意思|是爸爸不对|原谅我|爸爸错了",
    "question": r"为什么|为啥|是什么|怎么|你知道|[？?]\s*$|吗\s*$",
    "joke": r"哈哈|嘿嘿|逗你|开玩笑|好笑|笑死",
    "farewell": r"晚安|再见|拜拜|先睡了|我去忙|走了|下线",
    "greeting": r"早安|早上好|你好|在吗|我回来了|嗨|hello|\bhi\b",
    "promise": r"答应你|我保证|说好了|下次带你|一定带你|周末带你|明天带你|等你长大|约好|说话算话",
    "teach": r"教你|学着|试试|来试|你看|这样做|练习|自己来|跟我念|跟我学",
    "tired": r"好累|累了|累死|不舒服|头疼|心情不好|不开心|难受|烦死了",
    "call_dad": r"叫爸爸|叫我爸爸|喊爸爸|叫声爸爸",
    "explain": r"因为|所以|原因是|是这样的",
}
_COMPILED = {k: re.compile(v, re.IGNORECASE) for k, v in CUE_PATTERNS.items()}

# 二元组关键词时忽略的高频虚字
_STOP = set("的了是我你他她它们吗呢啊吧呀啦嘛哦嗯在有和就都也不这那一个么什怎为很还要会能")


def bigrams(text: str) -> set[str]:
    """中英文通用的轻量“词”集合：中文取字二元组，英文/数字取整词。"""
    out: set[str] = set()
    for seg in re.findall(r"[一-鿿]+|[a-zA-Z0-9]+", text.lower()):
        if re.match(r"[a-z0-9]+$", seg):
            if len(seg) >= 2:
                out.add(seg)
            continue
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg[0] in _STOP and bg[1] in _STOP:
                continue
            out.add(bg)
    return out


def extract_keywords(text: str, n: int = 6) -> list[str]:
    """优先不含虚字的二元组，保持出现顺序。"""
    seen: list[str] = []
    for seg in re.findall(r"[一-鿿]+|[a-zA-Z0-9]+", text.lower()):
        if re.match(r"[a-z0-9]+$", seg):
            if len(seg) >= 2 and seg not in seen:
                seen.append(seg)
            continue
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg not in seen:
                seen.append(bg)
    clean = [k for k in seen if not any(ch in _STOP for ch in k)]
    rest = [k for k in seen if k not in clean and not (k[0] in _STOP and k[1] in _STOP)]
    return (clean + rest)[:n]


def perceive(text: str, user_id: str, ts: float, gap_s: float | None = None,
             is_voice: bool = False) -> Percept:
    text = (text or "").strip()
    cues = {name for name, pat in _COMPILED.items() if pat.search(text)}
    return Percept(text=text, user_id=user_id, ts=ts, is_voice=is_voice,
                   cues=cues, keywords=extract_keywords(text), gap_s=gap_s)
