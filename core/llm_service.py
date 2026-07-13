"""统一模型路由、结构化生成和插件 Token 统计。"""
from __future__ import annotations

import json
import time
from typing import Any


class LLMService:
    """只接受 MaiBot 的任务路由名，不直接保存 Provider 凭据。"""

    def __init__(self, ctx: Any, config: Any, store: Any | None = None) -> None:
        self.ctx = ctx
        self.config = config
        self.store = store
        self.available_tasks: set[str] = set()
        self.health_error = "尚未检查"

    def update_config(self, config: Any) -> None:
        self.config = config

    def task_for(self, kind: str) -> str:
        models = self.config.models
        routes = {
            "fast": models.fast_task,
            "reasoning": models.reasoning_task,
            "creative": models.creative_task,
            "vision": models.vision_task,
            "schedule": models.schedule_task or models.reasoning_task,
            "rest_wakeup": models.rest_wakeup_task or models.fast_task,
            "continuity": models.continuity_task or models.fast_task,
            "dream": models.dream_task or models.creative_task,
            "vision_summary": models.vision_summary_task or models.vision_task,
            "diary": models.diary_task or models.creative_task,
            "date_analysis": models.date_analysis_task or models.fast_task,
            "skill": models.skill_task or models.fast_task,
            "news": models.news_task or models.fast_task,
            "search": models.search_task or models.reasoning_task,
            "relevance": models.relevance_task or models.reasoning_task,
            "group_judgment": models.group_judgment_task or models.fast_task,
            "relay_summary": models.relay_summary_task or models.fast_task,
            "creation_outline": models.creation_outline_task or models.reasoning_task,
            "creation_body": models.creation_body_task or models.creative_task,
            "creation_review": models.creation_review_task or models.reasoning_task,
            "reading_annotation": models.reading_annotation_task or models.creative_task,
        }
        return str(routes.get(kind) or models.reasoning_task or "planner").strip()

    async def refresh_health(self) -> set[str]:
        try:
            tasks = await self.ctx.llm.get_available_models()
            self.available_tasks = {str(item).strip() for item in tasks if str(item).strip()}
            self.health_error = "" if self.available_tasks else "Host 未返回可用任务"
        except Exception as exc:
            self.available_tasks = set()
            self.health_error = str(exc)[:200]
        return set(self.available_tasks)

    def task_available(self, kind: str) -> bool:
        task = self.task_for(kind)
        return bool(task and task in self.available_tasks)

    async def _record(self, *, result: dict[str, Any], task: str, request_type: str,
                      started: float, success: bool, error: str = "") -> None:
        if not self.store or not self.config.usage.enabled:
            return
        try:
            await self.store.record_llm_usage(
                created_at=time.time(), source="plugin", task_name=task,
                model_name=str(result.get("model") or result.get("model_name") or ""),
                request_type=request_type,
                prompt_tokens=int(result.get("prompt_tokens") or 0),
                completion_tokens=int(result.get("completion_tokens") or 0),
                total_tokens=int(result.get("total_tokens") or 0),
                latency_ms=(time.perf_counter()-started)*1000,
                success=success, error_summary=error,
            )
        except Exception as exc:
            self.ctx.logger.debug(f"[MaiLife] Token 统计写入失败: {exc}")

    async def generate(self, prompt: str | list[dict[str, Any]], system: str = "", max_tokens: int = 1800,
                       temperature: float = 0.5, *, task_kind: str = "reasoning",
                       request_type: str = "generic") -> str:
        task=self.task_for(task_kind)
        messages: str | list[dict[str, Any]] = prompt
        if isinstance(prompt,str) and system:
            messages=[{"role":"system","content":system},{"role":"user","content":prompt}]
        started=time.perf_counter(); result:dict[str,Any]={}
        try:
            result=await self.ctx.llm.generate(
                prompt=messages, model=task, temperature=temperature, max_tokens=max_tokens,
            )
            success=bool(isinstance(result,dict) and result.get("success") and result.get("response"))
            await self._record(result=result if isinstance(result,dict) else {},task=task,
                               request_type=request_type,started=started,success=success,
                               error="" if success else str((result or {}).get("error") or "empty_response"))
            if success:return str(result["response"]).strip()
        except Exception as exc:
            await self._record(result=result,task=task,request_type=request_type,started=started,success=False,error=str(exc))
            self.ctx.logger.warning(f"[MaiLife] LLM 调用失败 task={task} type={request_type}: {exc}")
        return ""

    async def generate_json(self, prompt: str, system: str, fallback: Any, max_tokens: int = 1800,
                            *, task_kind: str = "reasoning", request_type: str = "structured") -> Any:
        raw=await self.generate(prompt,system,max_tokens=max_tokens,task_kind=task_kind,request_type=request_type)
        if not raw:return fallback
        candidates=[raw]
        if "```" in raw:
            chunks=raw.split("```")
            candidates.extend(chunk.removeprefix("json").strip() for chunk in chunks[1::2])
        for left,right in (("[","]"),("{","}")):
            start,end=raw.find(left),raw.rfind(right)
            if start>=0 and end>start:candidates.append(raw[start:end+1])
        for text in candidates:
            try:return json.loads(text)
            except (json.JSONDecodeError,TypeError):continue
        self.ctx.logger.warning(f"[MaiLife] 结构化响应解析失败: {raw[:200]}")
        return fallback

    async def record_observed(self, *, source: str, task_name: str, request_type: str,
                              model_name: str, prompt_tokens: int, completion_tokens: int,
                              total_tokens: int, success: bool = True) -> None:
        if not self.store or not self.config.usage.enabled:return
        await self.store.record_llm_usage(
            created_at=time.time(),source=source,task_name=task_name,model_name=model_name,
            request_type=request_type,prompt_tokens=prompt_tokens,completion_tokens=completion_tokens,
            total_tokens=total_tokens,latency_ms=0,success=success,error_summary="",
        )
