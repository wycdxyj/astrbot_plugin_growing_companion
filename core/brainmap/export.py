"""脑图数据导出：节点=记忆，边=关联，颜色=情绪，大小=权重，亮度=最近被想起的程度。

只读：接收 ReadOnlyDB，纯计算，不写库、不触发回忆强化。
"""
from __future__ import annotations

import math
from typing import Any

from ..memory import dominant_emotion, effective_weight, row_to_memory
from ..models import EMOTION_CN, EMOTIONS

EMOTION_COLORS: dict[str, str] = {
    "joy": "#f7c948", "trust": "#5ec2a5", "fear": "#8e7cc3",
    "sadness": "#4a90d9", "curiosity": "#f28c38", "anger": "#e5484d",
}
NEUTRAL_COLOR = "#9aa5b1"


def export_graph(src: Any, now: float, *, max_nodes: int = 300, decay_speed: float = 1.0) -> dict[str, Any]:
    """返回 {nodes, edges, meta}。节点按权重取前 max_nodes（受保护的里程碑优先保留）。"""
    rows = [row_to_memory(r) for r in src.query("SELECT * FROM memories ORDER BY id DESC LIMIT 3000")]
    ranked = sorted(((effective_weight(m, now, decay_speed) + (0.5 if m["protected"] else 0.0), m) for m in rows),
                    key=lambda x: -x[0])
    picked = [m for _, m in ranked[:max_nodes]]
    ids = {m["id"] for m in picked}
    nodes: list[dict[str, Any]] = []
    for m in picked:
        w = effective_weight(m, now, decay_speed)
        vec = {e: round(float(m["emotion"].get(e, 0.0)), 3) for e in EMOTIONS}
        dom = dominant_emotion(m["emotion"]) if m["emotion"] and m["emotion_intensity"] > 0.15 else None
        last = m["last_recalled_at"] or m["created_at"]
        brightness = max(0.15, min(1.0, math.exp(-max(0.0, now - last) / 3600.0 / 72.0)))   # 越近被想起越亮
        nodes.append({
            "id": m["id"], "type": m["type"], "summary": (m["gist"] or m["content"]).replace("\n", " ")[:40],
            "scope_id": m.get('scope_id', 'legacy:father'), "source_kind": m.get('source_kind', 'experience'),
            "confidence": m.get('confidence', 0.7),
            "weight": round(w, 4), "size": round(2 + 10 * w, 2),
            "emotion": vec, "dominant_emotion": dom, "dominant_emotion_cn": EMOTION_CN[dom] if dom else "平静",
            "valence": round(m["valence"], 3), "arousal": round(m["arousal"], 3),
            "emotion_intensity": round(m["emotion_intensity"], 3),
            "color": EMOTION_COLORS.get(dom, NEUTRAL_COLOR) if dom else NEUTRAL_COLOR,
            "brightness": round(brightness, 3), "recall_count": m["recall_count"],
            "last_recalled_at": m["last_recalled_at"], "created_at": m["created_at"],
            "clarity": round(m["clarity"], 3), "compressed": bool(m["compressed"]),
            "compressed_into": m["compressed_into"], "protected": bool(m["protected"]), "dormant": bool(m["dormant"]),
        })
    edges: list[dict[str, Any]] = []
    if ids:
        for e in src.query("SELECT src,dst,kind,strength FROM memory_links ORDER BY strength DESC LIMIT 5000"):
            if e["src"] in ids and e["dst"] in ids:
                edges.append({"source": e["src"], "target": e["dst"], "kind": e["kind"],
                              "strength": round(float(e["strength"]), 3)})
    total_memories = src.query_one('SELECT COUNT(*) n FROM memories')['n']
    return {
        "nodes": nodes, "edges": edges,
        "meta": {"generated_at": now, "node_count": len(nodes), "edge_count": len(edges),
                 "total_memories": total_memories,
                 "truncated": total_memories > len(nodes), "readonly": True,
                 "emotion_colors": EMOTION_COLORS, "emotion_cn": EMOTION_CN},
    }
