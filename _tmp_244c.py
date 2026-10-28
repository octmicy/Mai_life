import io
p='tests/test_fixes.py'
with io.open(p,'r',encoding='utf-8',newline='') as f: raw=f.read()
crlf='\r\n' in raw; t=raw.replace('\r\n','\n')
lines=t.split('\n')
# 只 patch 确认是群闸门/群观察测试的 5 处（用更严格的上下文判定）
targets=[]
for i,l in enumerate(lines):
    s=l.strip()
    if s not in ('config.rest_gate.gate_mode = "schedule"','self.config.rest_gate.gate_mode = "schedule"'):
        continue
    receiver='config' if s.startswith('config.') else 'self.config'
    ctx='\n'.join(lines[max(0,i-6):i])
    is_group=('make_group_config' in ctx) or ('gate_segment_types' in ctx and 'GroupObserver' in ctx) or ('GroupObserver' in ctx and 'group' in ctx.casefold())
    if is_group:
        indent=l[:len(l)-len(l.lstrip())]
        lines[i]=f'{indent}{receiver}.group.gate_mode = "schedule"\n'+l
        targets.append((i+1,s[:12]))
assert len(targets)==5,('targets',targets)
t='\n'.join(lines)
if crlf: t=t.replace('\n','\r\n')
with io.open(p,'w',encoding='utf-8',newline='') as f: f.write(t)
print('patched:',targets)
