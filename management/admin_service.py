"""不暴露聊天原文和原始 API Key 的结构化管理摘要。"""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit,urlunsplit


_SCOPES={"overview","users","groups","dates","sources","bookshelf","tokens","proactive"}


def _safe_endpoint(value:Any)->str:
    text=str(value or "").strip()
    if not text:return ""
    try:
        parsed=urlsplit(text); host=parsed.hostname or ""
        if parsed.port:host=f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme,host,parsed.path,"",""))[:500]
    except ValueError:return "配置无效"


def _bounded_limit(value:Any,default:int=20)->int:
    try:parsed=int(value)
    except (TypeError,ValueError):parsed=default
    return max(1,min(100,parsed))


def _provider_id(index:int,provider:Any)->str:
    signature=f"{provider.provider_type}:{provider.endpoint}:{provider.model}"
    return f"p{index+1}-{provider.provider_type}-{hashlib.sha256(signature.encode()).hexdigest()[:8]}"


def _fingerprint(key:str)->str:return hashlib.sha256(str(key).encode("utf-8","ignore")).hexdigest()[:16]


def _redact_keys(value:Any,keys:list[str])->str:
    result=str(value or "")
    for key in keys:
        if key:result=result.replace(key,"[REDACTED]")
    return result


class AdminService:
    def __init__(self,store:Any,config:Any)->None:self.store=store; self.config=config
    def update_config(self,config:Any)->None:self.config=config

    async def snapshot(self,scope:str,now:Any,limit:int=20)->dict[str,Any]:
        normalized=str(scope or "overview").strip().lower()
        if normalized not in _SCOPES:normalized="overview"
        limit=_bounded_limit(limit)
        if normalized=="users":return {"scope":normalized,"items":await self._users(limit)}
        if normalized=="groups":return {"scope":normalized,"items":await self._groups(limit)}
        if normalized=="dates":return {"scope":normalized,"items":await self.store.management_date_candidates(limit)}
        if normalized=="sources":return {"scope":normalized,**await self._sources()}
        if normalized=="bookshelf":
            return {"scope":normalized,"items":await self.store.management_bookshelf(limit),
                    "creation_runs":await self.store.management_creation_runs(limit)}
        if normalized=="tokens":return {"scope":normalized,**await self._tokens(now)}
        if normalized=="proactive":return {"scope":normalized,"items":await self.store.management_proactive_candidates(limit)}
        return await self._overview(now)

    async def _users(self,limit:int)->list[dict[str,Any]]:
        users=await self.store.list_users()
        return [{"user_id":item["user_id"],"display_name":item.get("display_name") or "",
                 "role":item.get("role") or "friend","temperature":round(float(item.get("temperature") or 0),1),
                 "proactive_enabled":bool(item.get("proactive_enabled")),
                 "daily_proactive_max":int(item.get("daily_proactive_max") or 0),
                 "proactive_count":int(item.get("proactive_count") or 0),
                 "last_user_message_at":float(item.get("last_user_message_at") or 0),
                 "last_proactive_at":float(item.get("last_proactive_at") or 0)} for item in users[:limit]]

    async def _groups(self,limit:int)->list[dict[str,Any]]:
        directory={str(item["group_id"]):item for item in await self.store.list_group_directory(500)}
        result=[]
        for profile in self.config.social.groups[:limit]:
            group_id=str(profile.group_id); cached=directory.get(group_id,{})
            result.append({"group_id":group_id,"group_name":str(cached.get("group_name") or ""),
                           "stream_known":bool(cached.get("stream_id")),"enabled":bool(profile.enabled),
                           "observe_enabled":bool(profile.observe_enabled),
                           "relay_target_enabled":bool(profile.relay_target_enabled)})
        return result

    async def _sources(self)->dict[str,Any]:
        """组合配置与持久化健康状态，只输出 Key 指纹而不返回原始凭据。"""
        runtime={(str(item["provider_id"]),str(item["key_fingerprint"])):item
                 for item in await self.store.search_provider_health()}
        providers=[]
        for index,provider in enumerate(self.config.search_api.providers):
            provider_id=_provider_id(index,provider); keys=[]; raw_keys=[str(key) for key in provider.api_keys]
            for key in provider.api_keys:
                fingerprint=_fingerprint(key); state=runtime.get((provider_id,fingerprint),{})
                keys.append({"fingerprint":fingerprint,"status":str(state.get("status") or "healthy"),
                             "cooldown_until":float(state.get("cooldown_until") or 0),
                             "last_error_class":str(state.get("last_error_class") or "")})
            providers.append({"provider_id":provider_id,"provider_type":str(provider.provider_type),
                              "enabled":bool(provider.enabled),"key_count":len(keys),"keys":keys,
                              "endpoint":_redact_keys(_safe_endpoint(provider.endpoint),raw_keys),
                              "model":_redact_keys(provider.model,raw_keys)})
        return {"information_enabled":bool(self.config.information.enabled),"news_enabled":bool(self.config.news.enabled),
                "search_enabled":bool(self.config.search.enabled),"providers":providers,
                "external_reading":{"enabled":bool(self.config.creation.external_reading_enabled),
                                    "api_name":str(self.config.creation.external_reading_api_name or "")}}

    async def _tokens(self,now:Any)->dict[str,Any]:
        start=now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        return {"model_usage":await self.store.usage_summary(start),
                "search_api_usage":await self.store.search_api_summary(start)}

    async def _overview(self,now:Any)->dict[str,Any]:
        counts=await self.store.management_overview_counts(); usage=await self._tokens(now)
        tokens=usage["model_usage"]
        return {"scope":"overview","version":"1.7.2","users":counts.get("users",0),
                "owners":counts.get("owners",0),"pending_dates":counts.get("pending_dates",0),
                "bookshelf_documents":counts.get("bookshelf_documents",0),
                "private_documents":counts.get("private_documents",0),
                "pending_proactive":counts.get("pending_proactive",0),
                "token_calls_today":sum(int(item.get("calls") or 0) for item in tokens),
                "token_total_today":sum(int(item.get("total_tokens") or 0) for item in tokens),
                "search_calls_today":sum(int(item.get("calls") or 0) for item in usage["search_api_usage"]),
                "social_enabled":bool(self.config.social.enabled),"creation_enabled":bool(self.config.creation.enabled),
                "network_enabled":bool(self.config.information.enabled)}

    async def format_text(self,scope:str,now:Any,limit:int=12)->str:
        """将不同管理范围格式化为可复制文本，并保持敏感字段已经脱敏。"""
        data=await self.snapshot(scope,now,limit); scope=data["scope"]
        if scope=="overview":
            return ("Mai_life 管理概览\n"
                    f"用户 {data['users']}（主人 {data['owners']}）｜待确认日期 {data['pending_dates']}\n"
                    f"书柜 {data['bookshelf_documents']}（私人 {data['private_documents']}）｜待发送主动 {data['pending_proactive']}\n"
                    f"今日模型调用 {data['token_calls_today']}｜Token {data['token_total_today']}｜搜索 API 请求 {data['search_calls_today']}\n"
                    f"联网 {'开' if data['network_enabled'] else '关'}｜社交 {'开' if data['social_enabled'] else '关'}｜创作 {'开' if data['creation_enabled'] else '关'}")
        if scope=="users":
            lines=["用户角色与主动额度"]
            lines.extend(f"{item['user_id']} {item['display_name'] or '未读取昵称'}｜{item['role']}｜温度 {item['temperature']}｜主动 {item['proactive_count']}/{item['daily_proactive_max']}"
                         for item in data["items"])
        elif scope=="groups":
            lines=["QQ群配置"]
            lines.extend(f"{item['group_id']} {item['group_name'] or '未读取群名'}｜{'开' if item['enabled'] else '关'}｜观察 {'开' if item['observe_enabled'] else '关'}｜转述 {'开' if item['relay_target_enabled'] else '关'}"
                         for item in data["items"])
        elif scope=="dates":
            lines=["待确认日期"]+[f"#{item['id']} 用户 {item['user_id']}｜{item['event_name']}｜{item['date_text']}" for item in data["items"]]
        elif scope=="sources":
            lines=[f"联网服务：总开关 {'开' if data['information_enabled'] else '关'} / 新闻 {'开' if data['news_enabled'] else '关'} / 搜索 {'开' if data['search_enabled'] else '关'}"]
            for item in data["providers"]:
                states=",".join(f"{key['fingerprint']}:{key['status']}"+(f"/{key['last_error_class']}" if key['last_error_class'] else "") for key in item["keys"]) or "无 Key"
                lines.append(f"{item['provider_id']}｜{item['provider_type']}｜{'开' if item['enabled'] else '关'}｜Key {item['key_count']}｜{states}")
        elif scope=="bookshelf":
            lines=["书柜元数据（不含正文）"]+[f"{item['id']}｜{item['title']}｜{item['privacy']}｜{item['status']}" for item in data["items"]]
        elif scope=="tokens":
            lines=["今日模型 Token 聚合"]+[f"{item['source']}/{item['task_name']}｜{item['calls']} 次｜{int(item['total_tokens'] or 0)} Token" for item in data["model_usage"]]
            lines.append("今日搜索 API 请求（不计作 Token）")
            lines.extend(f"{item['provider_type']}｜{item['calls']} 次｜成功 {item['successes'] or 0}｜结果 {item['results'] or 0}" for item in data["search_api_usage"])
        else:
            lines=["近期主动候选"]+[f"{item['user_id']}｜{item.get('topic') or item['opportunity_id']}｜{item['status']}" for item in data["items"]]
        if len(lines)==1:lines.append("暂无记录。")
        return "\n".join(lines)
