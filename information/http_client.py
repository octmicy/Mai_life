"""无额外依赖的异步 HTTP 客户端，适配 Windows/Linux 与系统代理。"""
from __future__ import annotations

import asyncio
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


class HttpRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status:int
    url:str
    headers:dict[str,str]
    body:bytes

    def text(self)->str:
        content_type=self.headers.get("content-type","")
        charset="utf-8"
        if "charset=" in content_type:
            charset=content_type.split("charset=",1)[1].split(";",1)[0].strip() or "utf-8"
        for encoding in (charset,"utf-8","gb18030"):
            try:return self.body.decode(encoding)
            except (LookupError,UnicodeDecodeError):continue
        return self.body.decode("utf-8",errors="replace")


class HttpClient:
    def __init__(self,logger:Any)->None:self.logger=logger

    @staticmethod
    def validate_url(url:str)->str:
        value=str(url or "").strip(); parsed=urlparse(value)
        if parsed.scheme not in {"http","https"} or not parsed.netloc:
            raise HttpRequestError("只允许完整的 HTTP(S) 地址")
        return value

    async def get(self,url:str,*,timeout:float=8,max_bytes:int=2_000_000,
                  headers:dict[str,str]|None=None)->HttpResponse:
        target=self.validate_url(url)
        return await asyncio.to_thread(self._get_sync,target,timeout,max_bytes,headers or {})

    @staticmethod
    def _get_sync(url:str,timeout:float,max_bytes:int,headers:dict[str,str])->HttpResponse:
        request_headers={"User-Agent":"Mai_life/1.3 (+https://github.com/octmicy/Mai_life)",
                         "Accept-Encoding":"identity",**headers}
        request=urllib.request.Request(url,headers=request_headers,method="GET")
        context=ssl.create_default_context()
        try:
            with urllib.request.urlopen(request,timeout=max(1,float(timeout)),context=context) as response:
                body=response.read(max(1,int(max_bytes))+1)
                if len(body)>max_bytes:raise HttpRequestError(f"响应超过 {max_bytes} 字节限制")
                return HttpResponse(int(response.status),str(response.url),
                                    {str(k).lower():str(v) for k,v in response.headers.items()},body)
        except urllib.error.HTTPError as exc:
            if exc.code==304:
                return HttpResponse(304,url,{str(k).lower():str(v) for k,v in exc.headers.items()},b"")
            raise HttpRequestError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError,TimeoutError,OSError) as exc:
            reason=getattr(exc,"reason",exc)
            raise HttpRequestError(str(reason)[:200]) from exc
