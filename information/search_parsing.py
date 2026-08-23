"""联网搜索 API 协议解析与结果清洗。"""
from __future__ import annotations

import re
from typing import Any

from .http_client import HttpClient,HttpRequestError
from .search_models import SearchResponse,SearchResult

_URL_RE=re.compile(r"https?://[^\s<>\]\[()\"']+",re.I)


def _nested(value:Any,*paths:str)->Any:
    for path in paths:
        current=value
        for part in path.split("."):
            if not isinstance(current,dict):current=None; break
            current=current.get(part)
        if current is not None:return current
    return None


def error_from_payload(payload:Any)->str:
    if not isinstance(payload,dict):return ""
    code=str(payload.get("code") or payload.get("status") or "").casefold()
    error=payload.get("error")
    success=payload.get("success")
    # 显式成功（success=True 或 code 为明确成功值且无 error 字段）时直接返回，
    # 避免 message 中误含 "quota"/"balance"/"credit" 等词被当成错误。
    # 注意 code="" 属于歧义，不在此早返回，需继续走关键字分类以兼容只靠 message 报错的 Provider。
    if error is None and success is not False and code in {"0","200","ok","success"}:
        return ""
    if isinstance(error,dict):message=str(error.get("message") or error.get("type") or "")
    else:message=str(error or payload.get("message") or payload.get("msg") or "")
    text=(code+" "+message).casefold()
    if not text.strip():return ""
    if any(term in text for term in ("insufficient_quota","quota","credit","balance","exhausted","额度","余额")):return "quota"
    if any(term in text for term in ("unauthorized","authentication","invalid api key","invalid_api_key","鉴权","密钥无效")):return "auth"
    if any(term in text for term in ("rate limit","rate_limit","too many","限流")):return "rate_limit"
    if success is False or code not in {"","0","200","ok","success"}:return "provider_error"
    return ""


def clean_result(title:Any,url:Any,snippet:Any,generated:bool=False)->SearchResult|None:
    title_text=" ".join(str(title or "").split())[:500]
    snippet_text=" ".join(str(snippet or "").split())[:3000]
    url_text=str(url or "").strip()[:2000]
    if url_text:
        try:HttpClient.validate_url(url_text)
        except HttpRequestError:url_text=""
    if not title_text and not snippet_text:return None
    return SearchResult(title_text or "未命名结果",url_text,snippet_text,generated)


def parse_standard(provider_type:str,payload:Any,max_results:int)->SearchResponse:
    """把博查、Tavily 和 You.com 的不同字段归一化为统一搜索结果。"""
    if not isinstance(payload,dict):return SearchResponse([])
    if provider_type=="bocha":
        raw=_nested(payload,"data.webPages.value","data.web_pages.value","webPages.value","results")
        fields=("name","url","summary")
    elif provider_type=="tavily":
        raw=payload.get("results"); fields=("title","url","content")
    else:
        raw=_nested(payload,"hits","results","data.hits"); fields=("title","url","description")
    results=[]
    for item in raw if isinstance(raw,list) else []:
        if not isinstance(item,dict):continue
        snippet=(item.get(fields[2]) or item.get("snippet") or item.get("snippets")
                 or item.get("summary") or item.get("content") or "")
        if isinstance(snippet,list):snippet=" ".join(str(value) for value in snippet)
        result=clean_result(item.get(fields[0]) or item.get("name"),item.get(fields[1]),snippet)
        if result:results.append(result)
    limit=int(max_results)
    return SearchResponse(results[:limit],cited=any(item.url for item in results[:limit]))


def content_text(value:Any)->str:
    if isinstance(value,str):return value
    if not isinstance(value,list):return ""
    parts=[]
    for item in value:
        if isinstance(item,str):parts.append(item)
        elif isinstance(item,dict):
            text=item.get("text") or item.get("content")
            if isinstance(text,str):parts.append(text)
    return "\n".join(parts)


def parse_openai(provider_type:str,payload:Any,model:str,max_results:int)->SearchResponse:
    """解析 Responses/Chat 中转文本、引用和 Token；无 URL 时保留 Provider 生成标记。"""
    if not isinstance(payload,dict):return SearchResponse([])
    texts=[]; citations=[]
    if provider_type=="openai_responses":
        if isinstance(payload.get("output_text"),str):texts.append(payload["output_text"])
        for output in payload.get("output") if isinstance(payload.get("output"),list) else []:
            if not isinstance(output,dict):continue
            for content in output.get("content") if isinstance(output.get("content"),list) else []:
                if not isinstance(content,dict):continue
                text=content.get("text")
                if isinstance(text,str):texts.append(text)
                for annotation in content.get("annotations") if isinstance(content.get("annotations"),list) else []:
                    if isinstance(annotation,dict):citations.append(annotation)
    else:
        choices=payload.get("choices") if isinstance(payload.get("choices"),list) else []
        message=(choices[0].get("message") if choices and isinstance(choices[0],dict) else {})
        if isinstance(message,dict):
            text=content_text(message.get("content"))
            if text:texts.append(text)
            for key in ("citations","sources","web_search_results"):
                value=message.get(key)
                if isinstance(value,list):citations.extend(value)
    for key in ("citations","sources","web_search_results"):
        value=payload.get(key)
        if isinstance(value,list):citations.extend(value)
    generated="\n".join(part.strip() for part in texts if part.strip())[:12000]
    results=[]; seen=set()
    for citation in citations:
        if isinstance(citation,str):url=citation; title="外部引用"
        elif isinstance(citation,dict):
            nested=citation.get("url_citation") if isinstance(citation.get("url_citation"),dict) else citation
            url=str(nested.get("url") or nested.get("link") or ""); title=str(nested.get("title") or nested.get("name") or "外部引用")
        else:continue
        if url in seen:continue
        result=clean_result(title,url,generated[:1200],True)
        if result and result.url:results.append(result); seen.add(result.url)
    for url in _URL_RE.findall(generated):
        clean=url.rstrip(".,;:!?，。；：！？")
        if clean in seen:continue
        result=clean_result("模型返回的外部引用",clean,generated[:1200],True)
        if result and result.url:results.append(result); seen.add(result.url)
    if not results and generated:
        results=[SearchResult(f"{model} 联网结果", "", generated[:3000], True)]
    usage=payload.get("usage") if isinstance(payload.get("usage"),dict) else {}
    prompt=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total=int(usage.get("total_tokens") or prompt+completion)
    limit=int(max_results)
    return SearchResponse(results[:limit],generated_text=generated,cited=any(item.url for item in results),
                          model=str(payload.get("model") or model),prompt_tokens=prompt,
                          completion_tokens=completion,total_tokens=total)


def redact_key_echo(response:SearchResponse,key:str)->SearchResponse:
    """不信任服务返回内容；即使中转回显请求 Key，也不能让它进入缓存或 Prompt。"""
    secret=str(key or "")
    if not secret:return response
    def clean(value:str)->str:return str(value or "").replace(secret,"[REDACTED]")
    results=[SearchResult(
        clean(item.title),"" if secret in item.url else item.url,clean(item.snippet),item.provider_generated,
    ) for item in response.results]
    return SearchResponse(
        results,response.provider_id,response.provider_type,clean(response.generated_text),
        bool(response.cited and any(item.url for item in results)),
        clean(response.model),response.prompt_tokens,response.completion_tokens,response.total_tokens,
    )
