"""LLM 调用抽象层：插件不写死任何 API Key / 模型名 / 服务商地址，全部走 AstrBot 已配置的供应商。

- LLMAdapter：引擎只依赖这个协议
- AstrBotLLM：优先用 AstrBot ≥ v4.5.7 的 context.llm_generate（会话当前使用的聊天模型），
  不可用时回退到旧接口 context.get_using_provider().text_chat
- 可选 light_provider_id：意图判断/复盘等“轻量调用”可指定一个便宜的小模型（留空则与对话共用）
"""
from __future__ import annotations

from typing import Any, Protocol


class LLMAdapter(Protocol):
    async def chat(self, *, prompt: str, system_prompt: str, contexts: list[dict[str, str]],
                   purpose: str = "reply") -> str: ...


class AstrBotLLM:
    """把 AstrBot 的供应商接口适配成 LLMAdapter。每次对话用 event.unified_msg_origin 绑定会话。"""

    def __init__(self, context: Any, umo: str, light_provider_id: str = ""):
        self.context = context
        self.umo = umo
        self.light_provider_id = (light_provider_id or "").strip()

    async def chat(self, *, prompt: str, system_prompt: str, contexts: list[dict[str, str]],
                   purpose: str = "reply") -> str:
        ctx = self.context
        gen = getattr(ctx, "llm_generate", None)
        if gen is not None:      # 新接口（>= v4.5.7）
            pid = self.light_provider_id if (purpose != "reply" and self.light_provider_id) else \
                await ctx.get_current_chat_provider_id(umo=self.umo)
            resp = await gen(chat_provider_id=pid, prompt=prompt, system_prompt=system_prompt, contexts=contexts)
            return (getattr(resp, "completion_text", "") or "").strip()
        # 旧接口
        provider = None
        if purpose != "reply" and self.light_provider_id:
            provider = ctx.get_provider_by_id(self.light_provider_id)
        provider = provider or ctx.get_using_provider()
        if provider is None:
            raise RuntimeError("AstrBot 未配置可用的聊天模型供应商")
        resp = await provider.text_chat(prompt=prompt, system_prompt=system_prompt, contexts=contexts)
        return (getattr(resp, "completion_text", "") or "").strip()
