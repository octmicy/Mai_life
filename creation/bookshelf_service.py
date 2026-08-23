"""按主人/朋友边界读取书柜。"""
from __future__ import annotations

from typing import Any


_TYPE_LABELS={
    "novel_fragment":"小说片段","poem":"诗","essay":"随笔","screenplay":"短剧",
    "storyboard":"分镜脚本","character":"角色设定","worldbuilding":"世界观片段",
}


class BookshelfService:
    def __init__(self,store:Any,config:Any)->None:self.store=store; self.config=config
    def update_config(self,config:Any)->None:self.config=config

    @staticmethod
    def allow_private(user:dict[str,Any],is_admin:bool=False)->bool:
        return bool(is_admin or str(user.get("role") or "friend")=="owner")

    async def list_for_user(self,user:dict[str,Any],limit:int=20,*,is_admin:bool=False,
                            doc_type:str="")->list[dict[str,Any]]:
        return await self.store.list_bookshelf_documents(
            allow_private=self.allow_private(user,is_admin),limit=limit,doc_type=doc_type,
        )

    async def read_for_user(self,document_id:str,user:dict[str,Any],*,is_admin:bool=False)->dict[str,Any]:
        return await self.store.get_bookshelf_document(
            document_id,allow_private=self.allow_private(user,is_admin),
        )

    async def context_for_user(self,user:dict[str,Any],limit:int=3)->dict[str,Any]:
        rows=await self.list_for_user(user,limit)
        return {"items":[{"id":item["id"],"type":_TYPE_LABELS.get(item.get("work_type"),item.get("doc_type","文本")),
                          "title":item["title"],"summary":item.get("summary") or "",
                          "privacy":item["privacy"]} for item in rows]}

    @staticmethod
    def type_label(value:str)->str:return _TYPE_LABELS.get(value,value or "文本")
