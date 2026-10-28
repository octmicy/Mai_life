import io,re
p='tests/test_fixes.py'
with io.open(p,'r',encoding='utf-8',newline='') as f: raw=f.read()
crlf='\r\n' in raw; t=raw.replace('\r\n','\n')

lines=t.split('\n')
# 群相关 schedule 测试：rest_gate.gate_mode="schedule" 行后面紧跟群断言的，补 group.gate_mode
# 定位：5 处失败测试的设置行 1987 2010 2020（config.xxx），2202（self.config 观察侧）
# 逐行检查：若该行前面 4 行内出现 make_group_config 或 group.gate_segment_types → 属于群测试
changed=[]
for i,l in enumerate(lines):
    stripped=l.strip()
    if stripped=='config.rest_gate.gate_mode = "schedule"' or stripped=='self.config.rest_gate.gate_mode = "schedule"':
        receiver='config' if stripped.startswith('config.') else 'self.config'
        ctx='\n'.join(lines[max(0,i-6):i])
        is_group=('make_group_config' in ctx) or ('gate_segment_types' in ctx) or ('GroupObserver' in ctx)
        if is_group:
            indent=l[:len(l)-len(l.lstrip())]
            lines[i]=f'{indent}{receiver}.group.gate_mode = "schedule"\n'+l
            changed.append(i+1)
t='\n'.join(lines)
assert len(changed)==5,('changed',changed)
if crlf: t=t.replace('\n','\r\n')
with io.open(p,'w',encoding='utf-8',newline='') as f: f.write(t)
print('patched lines:',changed)
