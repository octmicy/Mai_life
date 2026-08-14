"""无第三方依赖的异步 HTTP 客户端，兼容 Windows 与 Linux。"""
from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin,urlparse


class HttpRequestError(RuntimeError):
    """HTTP 失败的结构化结果；异常文本不包含请求头、Key 或响应正文。"""

    def __init__(self,message:str,*,error_class:str="network",status_code:int=0,
                 headers:dict[str,str]|None=None,response_body:bytes=b"")->None:
        super().__init__(message)
        self.error_class=error_class; self.status_code=int(status_code)
        self.headers=headers or {}; self.response_body=response_body[:64_000]


@dataclass(frozen=True)
class HttpResponse:
    status:int
    url:str
    headers:dict[str,str]
    body:bytes

    def text(self)->str:
        content_type=self.headers.get("content-type",""); charset="utf-8"
        if "charset=" in content_type:
            charset=content_type.split("charset=",1)[1].split(";",1)[0].strip() or "utf-8"
        for encoding in (charset,"utf-8","gb18030"):
            try:return self.body.decode(encoding)
            except (LookupError,UnicodeDecodeError):continue
        return self.body.decode("utf-8",errors="replace")

    def json(self)->Any:
        try:return json.loads(self.text())
        except (json.JSONDecodeError,TypeError) as exc:
            raise HttpRequestError("接口未返回合法 JSON",error_class="invalid_response",
                                   status_code=self.status,headers=self.headers) from exc


def _validated_url(url:str)->str:
    value=str(url or "").strip(); parsed=urlparse(value)
    if parsed.scheme not in {"http","https"} or not parsed.netloc or not parsed.hostname:
        raise HttpRequestError("只允许完整的 HTTP(S) 地址",error_class="invalid_url")
    if parsed.username or parsed.password:
        raise HttpRequestError("地址不能包含用户凭据",error_class="invalid_url")
    return value


def _validate_public_url_sync(url:str)->tuple[str,str]:
    """解析域名并拒绝任何非公网地址，返回 (url, 选定的公网 IP) 供固定 IP 连接使用。"""
    value=_validated_url(url); host=str(urlparse(value).hostname or "").strip("[]").casefold()
    if host in {"localhost","localhost.localdomain"} or host.endswith(".localhost"):
        raise HttpRequestError("拒绝访问本机地址",error_class="unsafe_url")
    try:addresses=[ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses=list({ipaddress.ip_address(item[4][0].split("%",1)[0])
                            for item in socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)})
        except (socket.gaierror,OSError) as exc:
            raise HttpRequestError("域名解析失败",error_class="dns") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise HttpRequestError("拒绝访问内网、回环或保留地址",error_class="unsafe_url")
    pinned=next((str(address) for address in addresses if address.is_global),"")
    if not pinned:
        raise HttpRequestError("拒绝访问内网、回环或保留地址",error_class="unsafe_url")
    return value,pinned


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req:Any,fp:Any,code:int,msg:str,headers:Any,newurl:str)->Any:
        _validate_public_url_sync(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接到已校验的公网 IP；Host 头仍使用原域名，阻断 DNS rebinding。"""
    def __init__(self,host:str,port:int,*,pinned_ip:str,timeout:float)->None:
        super().__init__(host,port,timeout=timeout)
        self._pinned_ip=pinned_ip
    def connect(self)->None:
        self.sock=socket.create_connection((self._pinned_ip,self.port),self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接到已校验的公网 IP；TLS SNI 与 Host 头仍使用原域名，阻断 DNS rebinding。"""
    def __init__(self,host:str,port:int,*,pinned_ip:str,timeout:float,context:ssl.SSLContext)->None:
        super().__init__(host,port,timeout=timeout,context=context)
        self._pinned_ip=pinned_ip
    def connect(self)->None:
        sock=socket.create_connection((self._pinned_ip,self.port),self.timeout)
        if self._tunnel_host:
            self.sock=sock; self._tunnel()
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)


class HttpClient:
    def __init__(self,logger:Any)->None:self.logger=logger

    @staticmethod
    def validate_url(url:str)->str:return _validated_url(url)

    @staticmethod
    def validate_public_url(url:str)->str:return _validate_public_url_sync(url)[0]

    async def get(self,url:str,*,timeout:float=8,max_bytes:int=2_000_000,
                  headers:dict[str,str]|None=None,public_only:bool=False)->HttpResponse:
        return await self.request("GET",url,timeout=timeout,max_bytes=max_bytes,
                                  headers=headers,public_only=public_only)

    async def post_json(self,url:str,payload:Any,*,timeout:float=12,max_bytes:int=2_000_000,
                        headers:dict[str,str]|None=None)->HttpResponse:
        body=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8")
        merged={"Content-Type":"application/json",**(headers or {})}
        return await self.request("POST",url,body=body,timeout=timeout,max_bytes=max_bytes,headers=merged)

    async def request(self,method:str,url:str,*,body:bytes|None=None,timeout:float=8,
                      max_bytes:int=2_000_000,headers:dict[str,str]|None=None,
                      public_only:bool=False)->HttpResponse:
        """在线程中执行阻塞 urllib 请求，避免占用 MaiBot 的异步消息循环。"""
        target=self.validate_url(url)
        return await asyncio.to_thread(
            self._request_sync,str(method or "GET").upper(),target,body,float(timeout),
            int(max_bytes),headers or {},bool(public_only),
        )

    @staticmethod
    def _request_sync(method:str,url:str,body:bytes|None,timeout:float,max_bytes:int,
                      headers:dict[str,str],public_only:bool)->HttpResponse:
        """执行一次有大小上限的请求，并将网络/HTTP 失败归一化为不含 Key 的异常。"""
        if public_only:
            return HttpClient._request_public_sync(method,url,body,timeout,max_bytes,headers)
        target=_validated_url(url)
        request_headers={"User-Agent":"Mai_life/1.9.2 (+https://github.com/octmicy/Mai_life)",
                         "Accept-Encoding":"identity",**headers}
        request=urllib.request.Request(target,data=body,headers=request_headers,method=method)
        context=ssl.create_default_context()
        opener=urllib.request.build_opener(
            urllib.request.ProxyHandler(),urllib.request.HTTPSHandler(context=context),
        )
        try:
            with opener.open(request,timeout=max(1,timeout)) as response:
                # 多读一个字节用于可靠判断是否越过上限，避免把超大页面完整载入内存。
                response_body=response.read(max(1,max_bytes)+1)
                if len(response_body)>max_bytes:
                    raise HttpRequestError("响应超过大小限制",error_class="too_large",
                                           status_code=int(response.status))
                return HttpResponse(int(response.status),str(response.url),
                                    {str(k).lower():str(v) for k,v in response.headers.items()},response_body)
        except urllib.error.HTTPError as exc:
            response_headers={str(k).lower():str(v) for k,v in exc.headers.items()}
            try:response_body=exc.read(64_001)[:64_000]
            except Exception:response_body=b""
            status=int(exc.code); error_class=("auth" if status in {401,403} else
                "rate_limit" if status==429 else "server" if status>=500 else "http")
            raise HttpRequestError(f"HTTP {status}",error_class=error_class,status_code=status,
                                   headers=response_headers,response_body=response_body) from exc
        except HttpRequestError:raise
        except (TimeoutError,socket.timeout) as exc:
            raise HttpRequestError("请求超时",error_class="timeout") from exc
        except urllib.error.URLError as exc:
            reason=getattr(exc,"reason",None)
            error_class="timeout" if isinstance(reason,(TimeoutError,socket.timeout)) else "network"
            raise HttpRequestError("网络连接失败",error_class=error_class) from exc
        except (ssl.SSLError,OSError) as exc:
            raise HttpRequestError("网络连接失败",error_class="network") from exc

    @staticmethod
    def _request_public_sync(method:str,url:str,body:bytes|None,timeout:float,max_bytes:int,
                             headers:dict[str,str])->HttpResponse:
        """public_only 路径：解析一次并固定公网 IP 连接，逐跳复验重定向，阻断 DNS rebinding。"""
        request_headers={"User-Agent":"Mai_life/1.9.2 (+https://github.com/octmicy/Mai_life)",
                         "Accept-Encoding":"identity",**headers}
        context=ssl.create_default_context()
        target,pinned=_validate_public_url_sync(url)
        visited=0
        while True:
            parsed=urlparse(target)
            host=parsed.hostname; port=parsed.port or (443 if parsed.scheme=="https" else 80)
            path=parsed.path or "/"
            if parsed.query: path+="?"+parsed.query
            if parsed.scheme=="https":
                conn=_PinnedHTTPSConnection(host,port,pinned_ip=pinned,timeout=max(1,timeout),context=context)
            else:
                conn=_PinnedHTTPConnection(host,port,pinned_ip=pinned,timeout=max(1,timeout))
            try:
                conn.request(method,path,body=body,headers=request_headers)
                response=conn.getresponse()
                status=response.status
                if status in (301,302,303,307,308):
                    location=response.getheader("Location") or ""
                    response.read()
                    if not location:
                        raise HttpRequestError("重定向缺少 Location",error_class="network")
                    visited+=1
                    if visited>10:
                        raise HttpRequestError("重定向次数过多",error_class="network")
                    target=urljoin(target,location)
                    target,pinned=_validate_public_url_sync(target)
                    continue
                response_body=response.read(max(1,max_bytes)+1)
                if len(response_body)>max_bytes:
                    raise HttpRequestError("响应超过大小限制",error_class="too_large",
                                           status_code=int(status))
                return HttpResponse(int(status),target,
                                    {str(k).lower():str(v) for k,v in response.getheaders()},response_body)
            except HttpRequestError:raise
            except (TimeoutError,socket.timeout) as exc:
                raise HttpRequestError("请求超时",error_class="timeout") from exc
            except (ssl.SSLError,OSError,http.client.HTTPException) as exc:
                raise HttpRequestError("网络连接失败",error_class="network") from exc
            finally:
                try:conn.close()
                except Exception:pass
