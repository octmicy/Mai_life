import io
p='tests/test_fixes.py'
with io.open(p,'r',encoding='utf-8',newline='') as f: raw=f.read()
crlf='\r\n' in raw; t=raw.replace('\r\n','\n')
# 1) V1235：私聊模式断言仍写 rest_gate（正确），但类内共用配置——群相关测试已在 547 前补 group.gate_mode。
#    检查 547 是否已补：rest_gate 行之后应有 group.gate_mode
lines=t.split('\n')
i=next(i for i,l in enumerate(lines) if 'async def test_schedule_mode_group_gate_follows_segment' in l)
seg='\n'.join(lines[i:i+3])
if 'group.gate_mode' not in seg:
    # 在 rest_gate.gate_mode 行后插入
    j=i+1
    assert 'rest_gate.gate_mode' in lines[j], lines[j]
    lines[j:j+1]=[lines[j],'        self.config.group.gate_mode = "schedule"']
    t='\n'.join(lines)
    print('V1235 547 patched')
# 2) V1235 其余群观察测试（586 已补）；default_mode_is_time_window 断言 rest_gate 正确保留
# 3) summary group 键集（4495）
old='"group"), {"profiles", "gate_enabled", "gate_scope", "turn_enabled",'
assert t.count(old)==1,('set',t.count(old))
t=t.replace(old,'"group"), {"profiles", "gate_mode", "gate_enabled", "gate_scope", "turn_enabled",')
if crlf: t=t.replace('\n','\r\n')
with io.open(p,'w',encoding='utf-8',newline='') as f: f.write(t)
print('OK')
