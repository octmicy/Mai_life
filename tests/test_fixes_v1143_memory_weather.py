from __future__ import annotations

import tempfile
import unittest
from datetime import datetime,timedelta,timezone

from Mai_life.config import MaiLifeSettings,UserProfile
from Mai_life.core.environment import EnvironmentService
from Mai_life.core.storage import LifeStore
from Mai_life.life.memory_service import MemoryService


class DummyLLM:
    """无模型环境：task_available 恒 False，日记走 _generate_diary 兜底文案。"""
    def task_available(self,kind): return False
    async def generate_json(self,*args,**kwargs): return {}


class RecordingLogger:
    """记录各级别日志调用，供断言跳过说明与告警限流次数。"""
    def __init__(self):self.calls=[]
    def info(self,message,*args,**kwargs):self.calls.append(("info",str(message)))
    def warning(self,message,*args,**kwargs):self.calls.append(("warning",str(message)))
    def debug(self,message,*args,**kwargs):self.calls.append(("debug",str(message)))
    def error(self,message,*args,**kwargs):self.calls.append(("error",str(message)))
    def messages(self,level):return [text for lv,text in self.calls if lv==level]
    def __getattr__(self,name):return lambda *args,**kwargs:None


class StubHttpResponse:
    def __init__(self,payload:object)->None:self._payload=payload
    def json(self)->object:return self._payload


class RoutingHttp:
    """按 URL 分发的 HttpClient 替身：地理编码返回固定坐标，预报返回固定天气。

    fail_message 非空时任何请求都抛错，message 可改写以切换失败原因。
    """
    def __init__(self,forecast:object,fail_message:str="")->None:
        self.forecast=forecast; self.fail_message=fail_message; self.urls:list[str]=[]
    async def get(self,url:str,**kwargs:object)->StubHttpResponse:
        del kwargs
        self.urls.append(url)
        if self.fail_message:raise RuntimeError(self.fail_message)
        if "geocoding-api" in url:
            return StubHttpResponse({"results":[{"name":"上海","latitude":31.2,"longitude":121.5}]})
        return StubHttpResponse(self.forecast)


class DiaryBackfillTests(unittest.IsolatedAsyncioTestCase):
    """F1：补日记前必须确认目标日插件确在运行（daily_framework 非空），杜绝幻觉日记。"""

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.logger=RecordingLogger()
        self.service=MemoryService(self.store,MaiLifeSettings(),DummyLLM(),self.logger)
        self.now=datetime(2026,9,21,10,0,tzinfo=timezone(timedelta(hours=8)))
        self.target=self.now.date()-timedelta(days=1); self.day=self.target.isoformat()

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def _owner_only(self):
        opportunities=await self.store.active_opportunities(self.now.timestamp())
        return [item for item in opportunities if item["privacy"]=="owner_only"]

    async def test_fresh_install_skips_hallucinated_diary(self):
        """全新库首跑（昨日无任何 framework）→ 不补日记、不产生 owner_only 契机，并记录跳过说明。"""
        await self.store.sync_users([UserProfile(user_id="10001",role="owner",proactive_enabled=True)])
        await self.service.ensure_daily(self.now)
        self.assertEqual(await self.store.get_diary(self.day),{})
        self.assertEqual(await self._owner_only(),[])
        self.assertTrue(any(f"跳过补日记 day={self.day}" in text for text in self.logger.messages("info")),
                        f"未记录跳过说明: {self.logger.calls}")

    async def test_backfill_runs_when_yesterday_framework_exists(self):
        """合法补算场景（昨日框架存在，如凌晨停机、白天恢复）→ 正常生成日记与 owner_only 契机。"""
        await self.store.sync_users([UserProfile(user_id="10001",role="owner",proactive_enabled=True)])
        await self.store.replace_framework(self.day,[{"id":"n1","day":self.day,"start_minute":480,"end_minute":540,
            "kind":"meal","summary":"做早餐","location":"厨房","energy_load":-1,"shareability":0.3}])
        await self.service.ensure_daily(self.now)
        diary=await self.store.get_diary(self.day)
        self.assertEqual(diary["day"],self.day)
        # 无 LLM 时兜底标题来自变体池（F3），不再要求逐字相同。
        self.assertIn(diary["title"],{"普通的一天","平静的一天","如常的一天","寻常的一天","安静的一天"})
        self.assertEqual(len(await self._owner_only()),1)
        self.assertFalse(any("跳过补日记" in text for text in self.logger.messages("info")))


class WeatherWarningThrottleTests(unittest.IsolatedAsyncioTestCase):
    """F4：天气刷新失败告警按原因签名限流，同原因 30 分钟内只记 debug。"""

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=LifeStore(self.tmp.name); await self.store.initialize()
        self.config=MaiLifeSettings(); self.logger=RecordingLogger()
        self.http=RoutingHttp({"current":{"temperature_2m":30,"weather_code":0}},fail_message="city resolve timeout")

    async def asyncTearDown(self):
        await self.store.close(); self.tmp.cleanup()

    async def test_repeated_failure_warns_once_and_new_reason_warns_again(self):
        service=EnvironmentService(self.store,self.config,self.logger,http=self.http)
        first=await service.refresh_weather(force=True)
        self.assertEqual(first["description"],"天气未知"); self.assertEqual(first["location_name"],"Shanghai")
        second=await service.refresh_weather(force=True)
        self.assertEqual(second["description"],"天气未知")
        # 同原因连续失败：warning 只记一次，第二次降级为 debug（10 分钟一 tick 不再刷屏）。
        self.assertEqual(len(self.logger.messages("warning")),1)
        self.assertEqual(len(self.logger.messages("debug")),1)
        # 失败原因变化：签名不同，重新记 warning。
        self.http.fail_message="connection reset by peer"
        await service.refresh_weather(force=True)
        self.assertEqual(len(self.logger.messages("warning")),2)

    async def test_success_after_failure_resets_throttle_state(self):
        service=EnvironmentService(self.store,self.config,self.logger,http=self.http)
        await service.refresh_weather(force=True)
        self.assertEqual(len(self.logger.messages("warning")),1)
        # 网络恢复后限流状态清空：同原因再次失败立即重新 warning，避免恢复后长期静默。
        self.http.fail_message=""
        weather=await service.refresh_weather(force=True)
        self.assertEqual(weather["description"],"晴朗")
        self.http.fail_message="city resolve timeout"
        await service.refresh_weather(force=True)
        self.assertEqual(len(self.logger.messages("warning")),2)


if __name__=="__main__":unittest.main()
