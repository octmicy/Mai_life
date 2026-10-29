import io
p='tests/test_fixes.py'
with io.open(p,'r',encoding='utf-8',newline='') as f: raw=f.read()
crlf='\r\n' in raw; t=raw.replace('\r\n','\n')
def rep(t,old,new,count,label):
    n=t.count(old); assert n==count,(label,n,repr(old[:60])); return t.replace(old,new)

# ── 1) V1146BedtimeManagerTests：预武装回归用例（自持装配）──
anchor='    async def test_defer_cleared_when_asleep_woken_outside_window_or_disabled(self):'
assert t.count(anchor)==1,('anchor',t.count(anchor))
new_test='''    async def test_sleep_defer_is_prearmed_before_night_window(self):
        """v1.24.4 回归：夜窗前的消息必须预武装 defer，状态引擎不得在夜窗第一拍入睡。

        线上竞态：defer 只在夜窗内计算，状态推进先于睡前循环时相位立即入睡，
        协商条件（非睡眠相位）从此永假——睡前推迟 10 分钟彻底失效。
        """
        user_id="100040"
        await self.store.sync_users([UserProfile(user_id=user_id,role="owner")])
        now=datetime(2026,9,21,22,28,tzinfo=TZ)  # 夜窗（22:30）前 2 分钟
        day=now.strftime("%Y-%m-%d")
        await self.store.replace_framework(day,[
            {"id":"n","day":day,"start_minute":1350,"end_minute":480,"kind":"sleep",
             "summary":"夜间睡眠","location":"卧室","energy_load":0,"shareability":0.2}])
        await self.store.record_interaction(user_id,"还没睡呢",now.timestamp(),now.hour,
                                            source_message_id="m1")
        await self.manager.tick(now+timedelta(minutes=1))
        runtime=await self.store.get_sleep_runtime()
        defer_until=float(runtime.get("sleep_defer_until") or 0)
        self.assertGreater(defer_until,now.timestamp(),"夜窗前应预武装 defer（最后消息+10 分钟）")
        segment={"kind":"sleep","start_minute":1350,"summary":"夜间睡眠"}
        await self.state.advance(now+timedelta(minutes=2,seconds=5),segment,None)
        self.assertEqual((await self.store.get_sleep_runtime()).get("phase"),"awake",
                         "defer 未到期时状态引擎不得在夜窗第一拍入睡")
        gate=RestGate(self.store,self.config,DummyLLM(),self.state,DummyLogger())
        allowed,reason=await gate.decide(user_id,"再聊会儿嘛",now+timedelta(minutes=4),segment,
                                         session_id="s1",message_id="m2")
        self.assertTrue(allowed)
        self.assertIn("睡前对话未完",reason)

''' + anchor
t=rep(t,anchor,new_test,1,'prearm test')

# ── 2) defer_cleared：outside 分支按新语义重写（awake 时 defer 常驻预武装）──
old_out='''            "outside": (datetime(2026, 9, 21, 21, 0, tzinfo=TZ), "awake"),
        }
        for label, (now, phase) in cases.items():
            with self.subTest(case=label):
                runtime = await self.store.get_sleep_runtime()
                runtime.update({"phase": phase, "sleep_defer_until": now.timestamp() + 999})
                await self.store.save_sleep_runtime(runtime)
                self.env.now = lambda: now
                await self.manager.tick(now)
                self.assertEqual(await self._defer(), 0.0)'''
assert t.count(old_out)==1,('outside',t.count(old_out))
new_out='''        }
        for label, (now, phase) in cases.items():
            with self.subTest(case=label):
                runtime = await self.store.get_sleep_runtime()
                runtime.update({"phase": phase, "sleep_defer_until": now.timestamp() + 999})
                await self.store.save_sleep_runtime(runtime)
                self.env.now = lambda: now
                await self.manager.tick(now)
                self.assertEqual(await self._defer(), 0.0)
        # v1.24.4 预武装：awake 时 defer 常驻（夜窗外也维护，最后消息+静默分钟），
        # 闸门只在激活时消费该值；睡眠相位/叫醒/总闸关闭时仍清零。
        runtime = await self.store.get_sleep_runtime()
        runtime.update({"phase": "awake", "sleep_defer_until": 0})
        await self.store.save_sleep_runtime(runtime)
        outside = datetime(2026, 9, 21, 21, 0, tzinfo=TZ)
        self.env.now = lambda: outside
        await self.manager.tick(outside)
        self.assertGreater(await self._defer(), outside.timestamp())'''
t=rep(t,old_out,new_out,1,'outside semantics')

# ── 3) V1235GateModeTests 的 5 处 schedule 测试 + 547：群测试补 group.gate_mode ──
lines=t.split('\n')
sites=[]
for i,l in enumerate(lines):
    s=l.strip()
    if s in ('config.rest_gate.gate_mode = "schedule"','self.config.rest_gate.gate_mode = "schedule"'):
        receiver='config' if s.startswith('config.') else 'self.config'
        ctx='\n'.join(lines[max(0,i-6):i])
        if ('make_group_config' in ctx) or ('group.gate_segment_types' in ctx) or ('GroupObserver' in ctx):
            indent=l[:len(l)-len(l.lstrip())]
            lines[i]=f'{indent}{receiver}.group.gate_mode = "schedule"\n'+l
            sites.append(i+1)
t='\n'.join(lines)
assert len(sites)==5,('sites',sites)
# V1235 547：群 follows_segment 用例（无 make_group_config 上下文，单独锚定）
old547='''    async def test_schedule_mode_group_gate_follows_segment(self):
        self.config.rest_gate.gate_mode = "schedule"
        self.config.group.gate_enabled = True'''
assert t.count(old547)==1,('547',t.count(old547))
t=rep(t,old547,'''    async def test_schedule_mode_group_gate_follows_segment(self):
        self.config.rest_gate.gate_mode = "schedule"
        self.config.group.gate_mode = "schedule"
        self.config.group.gate_enabled = True''',1,'547')

if crlf: t=t.replace('\n','\r\n')
with io.open(p,'w',encoding='utf-8',newline='') as f: f.write(t)
print('all reapplied, sites:',sites)
