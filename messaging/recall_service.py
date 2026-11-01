"""撤回增强已删除（v1.24.7）。保留空壳类以维持 plugin.py 的 import 兼容。"""
from typing import Any


class RecallService:
    """全部方法为空操作：撤回增强功能已由主人指令删除。"""
    def __init__(self,ctx:Any,store:Any,config:Any,logger:Any)->None:pass
    def update_config(self,config:Any)->None:pass
    def clear(self)->None:pass
    def note_inbound(self,message:dict[str,Any])->None:pass
    async def register_turn(self,message:dict[str,Any],now:float|None=None)->None:pass
    async def record_notice(self,session_id:str,notice:dict[str,str],now:float|None=None)->dict[str,Any]:
        return {"message_id":"","user_id":"","group_id":"","anchors":[],"summary_cached":False,"needs_summary_recovery":False}
    async def is_turn_recalled(self,session_id:str,turn_anchor:str,now:float|None=None)->bool:return False
    async def planner_context(self,session_id:str,now:float|None=None,*,include_ids:bool=True)->str:return ""
    async def query_prompt_context(self,session_id:str,user_id:str,now:float|None=None)->str:return ""
    async def query_context(self,session_id:str,user_id:str,now:float|None=None)->dict[str,Any]:return {"enabled":False}
    async def recover_notice_summary(self,session_id:str,notice:dict[str,str],notice_at:float)->bool:return False
    async def register_reply_anchor(self,session_id:str,reply_anchor:str,source_message_ids:list[str],user_id:str,now:float|None=None)->None:pass
    @staticmethod
    def source_message_ids(message:dict[str,Any])->list[str]:
        return [str(message.get("message_id") or "")]
    def _retention_seconds(self)->int:return 0
    def _prune(self,now:float)->None:pass


def is_recall_query(text:str)->bool:return False
def recall_notice(message:dict[str,Any])->dict[str,str]:return {}
