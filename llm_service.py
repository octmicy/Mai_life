"""LLM helpers for structured narrative generation."""
from __future__ import annotations

import json
from typing import Any


class LLMService:
    def __init__(self, ctx: Any, config: Any) -> None:
        self.ctx = ctx
        self.config = config

    def update_config(self, config: Any) -> None:
        self.config = config

    async def generate(self, prompt: str, system: str, max_tokens: int = 1800, temperature: float = 0.5) -> str:
        try:
            result = await self.ctx.llm.generate(
                prompt=[{"role":"system","content":system},{"role":"user","content":prompt}],
                model=self.config.plugin.llm_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if isinstance(result, dict) and result.get("success") and result.get("response"):
                return str(result["response"]).strip()
        except Exception as exc:
            self.ctx.logger.warning(f"[MaiLife] LLM 调用失败: {exc}")
        return ""

    # 依次尝试全文、代码块和最外层 JSON；全部失败才返回规则降级值。
    async def generate_json(self, prompt: str, system: str, fallback: Any, max_tokens: int = 1800) -> Any:
        raw = await self.generate(prompt, system, max_tokens=max_tokens)
        if not raw:
            return fallback
        candidates=[raw]
        if "```" in raw:
            chunks=raw.split("```")
            candidates.extend(chunk.removeprefix("json").strip() for chunk in chunks[1::2])
        left_list,right_list=raw.find("["),raw.rfind("]")
        if left_list>=0 and right_list>left_list: candidates.append(raw[left_list:right_list+1])
        left_obj,right_obj=raw.find("{"),raw.rfind("}")
        if left_obj>=0 and right_obj>left_obj: candidates.append(raw[left_obj:right_obj+1])
        for text in candidates:
            try:
                return json.loads(text)
            except (json.JSONDecodeError,TypeError):
                continue
        self.ctx.logger.warning(f"[MaiLife] 结构化响应解析失败: {raw[:200]}")
        return fallback
