import io
# 1) test_contract.py：group 键集加 gate_mode
p='tests/test_contract.py'
with io.open(p,'r',encoding='utf-8',newline='') as f: raw=f.read()
crlf='\r\n' in raw; t=raw.replace('\r\n','\n')
def rep(t,old,new,count,label):
    n=t.count(old); assert n==count,(label,n,repr(old[:60])); return t.replace(old,new)
old='"gate_enabled","gate_scope","turn_enabled",\n                                             "gate_segment_types","night_window","nap_window"})'
assert t.count(old)==1,('contract group set',t.count(old))
t=rep(t,old,'"gate_mode","gate_enabled","gate_scope","turn_enabled",\n                                             "gate_segment_types","night_window","nap_window"})',1,'contract set')
if crlf: t=t.replace('\n','\r\n')
with io.open(p,'w',encoding='utf-8',newline='') as f: f.write(t)
print('test_contract OK')

# 2) test_fixes.py：SECTIONS 组键集 + 回归用例
p='tests/test_fixes.py'
with io.open(p,'r',encoding='utf-8',newline='') as f: raw=f.read()
crlf='\r\n' in raw; t=raw.replace('\r\n','\n')
old='"gate_enabled", "gate_scope", "turn_enabled",\n                                              "gate_segment_types", "night_window", "nap_window",'
assert t.count(old)==1,('fixes group set',t.count(old))
t=rep(t,old,'"gate_mode", "gate_enabled", "gate_scope", "turn_enabled",\n                                              "gate_segment_types", "night_window", "nap_window",',1,'fixes set')
# 回归用例：群闸门独立 mode
anchor='    async def test_time_windows_are_gated_by_segment_type_list(self):'
assert t.count(anchor)==1
indep=anchor.replace('    async def test_time_windows',
'''    async def test_group_gate_mode_is_independent_from_private(self):
        """v1.24.4 回归：群闸门生效方式独立于私聊（group.gate_mode），旧配置迁移继承。"""
        from Mai_life.life.rest_gate import RestGate
        config = make_group_config(group_gate=True)
        config.group.gate_segment_types = ["sleep", "nap"]
        config.rest_gate.gate_mode = "schedule"       # 私聊=按日程
        config.group.gate_mode = "time_window"        # 群聊=按设定时间（独立选择）
        gate = RestGate(self.store, config, DummyLLM(), DummyStateEngine(), DummyLogger())
        noon = NIGHT.replace(hour=12, minute=30)
        allowed, _ = gate.decide_group("中午聊天", noon, "100", {"kind": "leisure"})
        self.assertFalse(allowed, "群 time_window 模式下午窗不看段型，应静默")
        # 群改 schedule + nap 移除 → 中午放行（私聊仍是 schedule 不受影响）
        config.group.gate_mode = "schedule"
        config.group.gate_segment_types = ["sleep"]
        allowed2, _ = gate.decide_group("中午聊天", noon, "100", {"kind": "nap"})
        self.assertTrue(allowed2, "群 schedule 模式 nap 移除后中午应放行")

''' + '    async def test_time_windows')
t=rep(t,anchor,indep,1,'independent test')
if crlf: t=t.replace('\n','\r\n')
with io.open(p,'w',encoding='utf-8',newline='') as f: f.write(t)
print('test_fixes OK')
