"""疑难图片的短视觉摘要；失败时始终交回 MaiBot 原生多模态。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import time
from typing import Any

from .adapter_compat import component_kind, reply_target_ids
from .message_pipeline import _walk_components, media_bytes, media_types, plain_text


def _image_bytes(item:dict[str,Any],max_bytes:int=0) -> bytes:
    raw=item.get("binary_data_base64") or item.get("base64") or item.get("image_base64") or ""
    if not isinstance(raw,str) or not raw:return b""
    if raw.startswith("data:") and "," in raw:raw=raw.split(",",1)[1]
    if max_bytes and len(raw)*3//4>max_bytes:return b""
    try:
        data=base64.b64decode(raw+"="*(-len(raw)%4))
        return data if not max_bytes or len(data)<=max_bytes else b""
    except Exception:return b""


def _format(data:bytes) -> str:
    if data.startswith((b"GIF87a",b"GIF89a")):return "gif"
    if data.startswith(b"\x89PNG"):return "png"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:return "webp"
    return "jpeg"


class VisionService:
    def __init__(self, ctx:Any, store:Any, config:Any, llm:Any, logger:Any) -> None:
        self.ctx=ctx; self.store=store; self.config=config; self.llm=llm; self.logger=logger

    def update_config(self,config:Any)->None:self.config=config

    async def _quoted_images(self,message:dict[str,Any])->list[dict[str,Any]]:
        ids=reply_target_ids(message)
        images=[]; max_bytes=int(self.config.debounce.max_media_bytes)
        for mid in ids[:2]:
            try:
                result=await self.ctx.message.get_by_id(mid,stream_id=str(message.get("session_id") or ""),include_binary_data=True)
                candidates=[]
                if isinstance(result,dict):
                    candidates.append(result)
                    candidates.extend(value for value in result.values() if isinstance(value,dict))
                for candidate in candidates:
                    for item in _walk_components(candidate.get("raw_message") or candidate.get("message") or []):
                        if component_kind(item)=="image" and _image_bytes(item,max_bytes):images.append(item)
            except Exception as exc:self.logger.debug(f"[MaiLife] 引用图片读取失败 message={mid}: {exc}")
        return images

    def _gif_frames(self,data:bytes)->list[tuple[str,str]]:
        try:
            from PIL import Image,ImageOps
        except Exception:return [("gif",base64.b64encode(data).decode())]
        frames=[]
        try:
            with Image.open(io.BytesIO(data)) as image:
                count=max(1,int(getattr(image,"n_frames",1)))
                max_frames=int(self.config.vision.gif_max_frames)
                indices=sorted({round(index*(count-1)/max(1,max_frames-1)) for index in range(min(count,max_frames))})
                for index in indices:
                    image.seek(index); frame=ImageOps.exif_transpose(image.convert("RGBA"))
                    background=Image.new("RGBA",frame.size,(255,255,255,255)); background.alpha_composite(frame)
                    rgb=background.convert("RGB"); rgb.thumbnail((1024,1024))
                    output=io.BytesIO(); rgb.save(output,format="JPEG",quality=82)
                    frames.append(("jpeg",base64.b64encode(output.getvalue()).decode()))
        except Exception:return [("gif",base64.b64encode(data).decode())]
        return frames or [("gif",base64.b64encode(data).decode())]

    @staticmethod
    def _parse(raw:str)->dict[str,str]:
        candidates=[raw]
        if "```" in raw:candidates.extend(chunk.removeprefix("json").strip() for chunk in raw.split("```")[1::2])
        left,right=raw.find("{"),raw.rfind("}")
        if left>=0 and right>left:candidates.append(raw[left:right+1])
        for candidate in candidates:
            try:
                value=json.loads(candidate)
                if isinstance(value,dict):
                    return {"summary":str(value.get("summary") or ""),"intent":str(value.get("intent") or ""),
                            "ownership_hint":str(value.get("ownership_hint") or "")}
            except (json.JSONDecodeError,TypeError):pass
        return {"summary":raw.strip(),"intent":"","ownership_hint":""}

    async def summarize_if_needed(self,message:dict[str,Any])->str:
        cfg=self.config.vision
        if not cfg.enabled or not self.llm.task_available("vision_summary"):return ""
        max_bytes=int(self.config.debounce.max_media_bytes)
        if media_bytes(message)>max_bytes:return ""
        types=media_types(message); text=plain_text(message)
        direct=[item for item in _walk_components(message.get("raw_message") or [])
                if component_kind(item)=="image" and _image_bytes(item,max_bytes)]
        source_type="direct"
        info=message.get("message_info") if isinstance(message.get("message_info"),dict) else {}
        additional=info.get("additional_config") if isinstance(info.get("additional_config"),dict) else {}
        merged_ids=additional.get("mai_life_merged_message_ids")
        merged_image=bool(direct and isinstance(merged_ids,list) and len(merged_ids)>1)
        difficult=(len(direct)==1 and not text) or merged_image or "gif" in types or "forward" in types or "reply" in types
        if not difficult:return ""
        if "reply" in types:
            quoted=await self._quoted_images(message)
            if quoted:direct.extend(quoted); source_type="quoted"
        elif "forward" in types:source_type="forward"
        elif "gif" in types:source_type="gif"
        direct=direct[:int(cfg.max_images)]
        if not direct:return ""
        payload=[]; hashes=[]; remaining_bytes=max_bytes
        for item in direct:
            data=_image_bytes(item,max_bytes)
            if not data or len(data)>remaining_bytes:continue
            remaining_bytes-=len(data)
            digest=str(item.get("hash") or hashlib.sha256(data).hexdigest())
            hashes.append(digest)
            if _format(data)=="gif":payload.extend(self._gif_frames(data))
            else:payload.append((_format(data),base64.b64encode(data).decode()))
        if not payload:return ""
        combined=hashlib.sha256("|".join(hashes).encode()).hexdigest()
        now=time.time(); cached=await self.store.get_image_summary(combined,now)
        if cached:
            await self.store.save_image_summary(
                combined,str(cached.get("summary") or ""),source_type,str(cached.get("ownership_hint") or ""),
                str(message.get("session_id") or ""),now,float(cached.get("expires_at") or now),
                now+int(cfg.current_pointer_minutes)*60,
            )
            return str(cached.get("summary") or "")
        content:list[dict[str,Any]]=[{"type":"text","text":(
            "请理解这些图片，只返回JSON：{\"summary\":\"80到180字客观视觉摘要\","
            "\"intent\":\"可能表达的聊天意图\",\"ownership_hint\":\"来源/归属线索\"}。"
            "不要猜测人物真实身份，不要把可能性写成事实。"
        )}]
        for fmt,data in payload[:max(int(cfg.max_images),int(cfg.gif_max_frames))]:
            content.append({"type":"image","image_format":fmt,"image_base64":data})
        prompt=[{"role":"system","content":"你是克制的图片转述器，只输出合法JSON。"},{"role":"user","content":content}]
        try:
            raw=await asyncio.wait_for(
                self.llm.generate(prompt,max_tokens=420,temperature=0.2,task_kind="vision_summary",request_type="vision_summary"),
                timeout=float(cfg.timeout_seconds),
            )
        except asyncio.TimeoutError:
            self.logger.info("[MaiLife] 疑难图片摘要超时，交回原生多模态")
            return ""
        if not raw:return ""
        parsed=self._parse(raw); summary=parsed["summary"].strip()[:1000]
        if not summary:return ""
        await self.store.save_image_summary(
            combined,summary,source_type,parsed["ownership_hint"],str(message.get("session_id") or ""),now,
            now+int(cfg.summary_ttl_hours)*3600,now+int(cfg.current_pointer_minutes)*60,
        )
        return summary
