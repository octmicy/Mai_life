"""不暴露聊天原文和密钥的结构化管理摘要。"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit,urlunsplit


_SCOPES={"overview","users","relations","dates","sources","bookshelf","tokens","proactive"}


def _safe_endpoint(value:Any)->str:
    """管理摘要保留连接位置但去掉查询参数和凭据。"""
    text=str(value or "").strip()
    if not text:return ""
    try:
        parsed=urlsplit(text)
        host=parsed.hostname or ""
        if parsed.port:host=f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme,host,parsed.path,"",""))[:500]
    except ValueError:return "配置无效"


def _bounded_limit(value:Any,default:int=20)->int:
    try:parsed=int(value)
    except (TypeError,ValueError):parsed=default
    return max(1,min(100,parsed))


class AdminService:
    def __init__(self,store:Any,config:Any)->None:self.store=store; self.config=config
    def update_config(self,config:Any)->None:self.config=config

    async def snapshot(self,scope:str,now:Any,limit:int=20)->dict[str,Any]:
        normalized=str(scope or "overview").strip().lower()
        if normalized not in _SCOPES:normalized="overview"
        limit=_bounded_limit(limit)
        if normalized=="users":return {"scope":normalized,"items":await self._users(limit)}
        if normalized=="relations":return {"scope":normalized,"items":await self.store.management_relationship_entries(limit)}
        if normalized=="dates":return {"scope":normalized,"items":await self.store.management_date_candidates(limit)}
        if normalized=="sources":return {"scope":normalized,**self._sources()}
        if normalized=="bookshelf":
            return {"scope":normalized,"items":await self.store.management_bookshelf(limit),
                    "creation_runs":await self.store.management_creation_runs(limit)}
        if normalized=="tokens":return {"scope":normalized,"items":await self._tokens(now)}
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

    def _sources(self)->dict[str,Any]:
        news=[]
        for item in self.config.news.sources:
            news.append({"source_id":str(item.source_id),"name":str(item.name),"type":str(item.source_type),
                         "enabled":bool(item.enabled),"endpoint":_safe_endpoint(item.url),
                         "api_name":str(item.api_name or "")})
        return {"information_enabled":bool(self.config.information.enabled),"news_enabled":bool(self.config.news.enabled),
                "search_enabled":bool(self.config.search.enabled),"news_sources":news,
                "search":{"connector":str(self.config.search.connector),
                          "endpoint":_safe_endpoint(self.config.search.endpoint)},
                "external_reading":{"enabled":bool(self.config.creation.external_reading_enabled),
                                    "api_name":str(self.config.creation.external_reading_api_name or "")}}

    async def _tokens(self,now:Any)->list[dict[str,Any]]:
        start=now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        return await self.store.usage_summary(start)

    async def _overview(self,now:Any)->dict[str,Any]:
        counts=await self.store.management_overview_counts()
        tokens=await self._tokens(now)
        return {"scope":"overview","version":"1.6.0","users":counts.get("users",0),
                "owners":counts.get("owners",0),"pending_dates":counts.get("pending_dates",0),
                "bookshelf_documents":counts.get("bookshelf_documents",0),
                "private_documents":counts.get("private_documents",0),
                "pending_proactive":counts.get("pending_proactive",0),
                "token_calls_today":sum(int(item.get("calls") or 0) for item in tokens),
                "token_total_today":sum(int(item.get("total_tokens") or 0) for item in tokens),
                "social_enabled":bool(self.config.social.enabled),"creation_enabled":bool(self.config.creation.enabled),
                "network_enabled":bool(self.config.information.enabled)}

    async def format_text(self,scope:str,now:Any,limit:int=12)->str:
        data=await self.snapshot(scope,now,limit); scope=data["scope"]
        if scope=="overview":
            return ("Mai_life 管理概览\n"
                    f"用户 {data['users']}（主人 {data['owners']}）｜待确认日期 {data['pending_dates']}\n"
                    f"书柜 {data['bookshelf_documents']}（私人 {data['private_documents']}）｜待发送主动 {data['pending_proactive']}\n"
                    f"今日模型调用 {data['token_calls_today']}｜Token {data['token_total_today']}\n"
                    f"联网 {'开' if data['network_enabled'] else '关'}｜社交 {'开' if data['social_enabled'] else '关'}｜创作 {'开' if data['creation_enabled'] else '关'}")
        if scope=="users":
            lines=["用户角色与主动额度"]
            lines.extend(f"{item['user_id']} {item['role']}｜温度 {item['temperature']}｜主动 {item['proactive_count']}/{item['daily_proactive_max']}"
                         for item in data["items"])
        elif scope=="relations":
            lines=["群友关系词条"]+[f"{item['group_alias']} / {item['alias']} -> {item['user_id']}" for item in data["items"]]
        elif scope=="dates":
            lines=["待确认日期"]+[f"#{item['id']} 用户 {item['user_id']}｜{item['event_name']}｜{item['date_text']}" for item in data["items"]]
        elif scope=="sources":
            lines=[f"来源状态：联网 {'开' if data['information_enabled'] else '关'} / 新闻 {'开' if data['news_enabled'] else '关'} / 搜索 {'开' if data['search_enabled'] else '关'}"]
            lines.extend(f"{item['source_id']} {item['type']}｜{'开' if item['enabled'] else '关'}｜{item['endpoint'] or item['api_name']}" for item in data["news_sources"])
            lines.append(f"搜索 {data['search']['connector']}｜{data['search']['endpoint'] or '未配置'}")
        elif scope=="bookshelf":
            lines=["书柜元数据（不含正文）"]+[f"{item['id']}｜{item['title']}｜{item['privacy']}｜{item['status']}" for item in data["items"]]
        elif scope=="tokens":
            lines=["今日 Token 聚合"]+[f"{item['source']}/{item['task_name']}｜{item['calls']} 次｜{int(item['total_tokens'] or 0)} Token" for item in data["items"]]
        else:
            lines=["近期主动候选"]+[f"{item['user_id']}｜{item.get('topic') or item['opportunity_id']}｜{item['status']}" for item in data["items"]]
        if len(lines)==1:lines.append("暂无记录。")
        return "\n".join(lines)
