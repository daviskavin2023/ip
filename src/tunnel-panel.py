#!/usr/bin/env python3
import json, os, subprocess, urllib.request, urllib.error, time, threading, ssl
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
CFG='/etc/tunnel-panel/config.env'; BACKUP_DIR='/etc/tunnel-panel/backups'; CACHE_FILE='/var/lib/tunnel-panel/health-cache.json'; SAVE_FILE='/var/lib/tunnel-panel/save-status.json'; LOCK=threading.Lock(); SAVE_LOCK=threading.Lock()
def env():
 d={}
 try:
  for l in open(CFG):
   l=l.strip()
   if l and not l.startswith('#') and '=' in l:
    k,v=l.split('=',1); d[k]=v.strip().strip('"').strip("'")
 except: pass
 return d
CONFIG=env(); HOST=CONFIG.get('PANEL_HOST','0.0.0.0'); PORT=int(CONFIG.get('PANEL_PORT','39091'))
def save_env(tok,acc):
 os.makedirs(os.path.dirname(CFG),exist_ok=True); open(CFG,'w').write(f"CF_API_TOKEN={tok.strip()}\nCF_ACCOUNT_ID={acc.strip()}\nPANEL_HOST=0.0.0.0\nPANEL_PORT=39091\n"); os.chmod(CFG,0o600)
def run(c):
 try: p=subprocess.run(c,shell=True,text=True,capture_output=True,timeout=8); return p.stdout.strip()
 except Exception as e: return str(e)
def api(path,method='GET',body=None):
 tok=CONFIG.get('CF_API_TOKEN','')
 if not tok: return {'success':False,'errors':[{'message':'CF_API_TOKEN not configured'}]}
 data=json.dumps(body).encode() if body is not None else None
 req=urllib.request.Request('https://api.cloudflare.com/client/v4'+path,data=data,method=method); req.add_header('Authorization','Bearer '+tok); req.add_header('Content-Type','application/json')
 try:
  with urllib.request.urlopen(req,timeout=25) as r: return json.loads(r.read().decode())
 except urllib.error.HTTPError as e:
  try: o=json.loads(e.read().decode())
  except: o={'success':False,'errors':[{'message':str(e)}]}
  return o
 except Exception as e: return {'success':False,'errors':[{'message':str(e)}]}
def acc(): return CONFIG.get('CF_ACCOUNT_ID','')
def tunnels(): return api(f'/accounts/{acc()}/cfd_tunnel?is_deleted=false&per_page=200') if acc() else {'success':False,'errors':[{'message':'CF_ACCOUNT_ID not configured'}]}
def tcfg(tid): return api(f'/accounts/{acc()}/cfd_tunnel/{tid}/configurations')
def update(tid,config): return api(f'/accounts/{acc()}/cfd_tunnel/{tid}/configurations','PUT',{'config':config})
def check(svc):
 if not svc or not (svc.startswith('http://') or svc.startswith('https://')): return {'checked':False,'note':'非 HTTP'}
 st=time.time()
 try:
  ctx=ssl._create_unverified_context() if svc.startswith('https://') else None
  req=urllib.request.Request(svc); req.add_header('User-Agent','tunnel-panel/1.0')
  with urllib.request.urlopen(req,timeout=4,context=ctx) as r: return {'checked':True,'ok':True,'status':r.status,'ms':int((time.time()-st)*1000)}
 except urllib.error.HTTPError as e: return {'checked':True,'ok':True,'status':e.code,'ms':int((time.time()-st)*1000),'note':'reachable'}
 except Exception as e: return {'checked':True,'ok':False,'ms':int((time.time()-st)*1000),'error':str(e)}
def rows(with_health):
 ts=tunnels()
 if not ts.get('success'): return ts
 out=[]
 for t in ts.get('result') or []:
  c=tcfg(t.get('id'))
  if not c.get('success'): continue
  conf=((c.get('result') or {}).get('config') or {})
  for it in conf.get('ingress') or []:
   h=it.get('hostname',''); svc=it.get('service',''); path=it.get('path','')
   if h: out.append({'tunnel_id':t.get('id'),'tunnel':t.get('name'),'tunnelStatus':t.get('status'),'hostname':h,'path':path,'service':svc,'health':check(svc) if with_health else {'checked':False,'note':'等待巡检'}})
 return {'success':True,'checked_at':time.strftime('%F %T'),'running':False,'result':out}
def notify_3040_sync():
 def _call():
  try:
   req=urllib.request.Request('http://127.0.0.1:3040/api/sync',data=b'{}',headers={'Content-Type':'application/json'})
   urllib.request.urlopen(req,timeout=4)
  except: pass
 threading.Thread(target=_call,daemon=True).start()

def force_refresh_now():
 o=rows(False)
 if o.get('success'):
  o['checked_at']=time.strftime('%F %T')
  o['running']=False
  write_cache(o)
  refresh_bg()
  notify_3040_sync()
 return o
def read_cache():
 try: return json.loads(open(CACHE_FILE).read())
 except: return rows(False)
def write_cache(o):
 os.makedirs(os.path.dirname(CACHE_FILE),exist_ok=True); tmp=CACHE_FILE+'.tmp'; open(tmp,'w').write(json.dumps(o,ensure_ascii=False,indent=2)); os.replace(tmp,CACHE_FILE)
def refresh_bg():
 if not LOCK.acquire(False): return False
 def work():
  try:
   old=read_cache(); old['running']=True; old['checked_at']=time.strftime('%F %T'); write_cache(old); write_cache(rows(True))
  finally: LOCK.release()
 threading.Thread(target=work,daemon=True).start(); return True
def zone_for_hostname(host):
 parts=host.strip('.').split('.')
 # Try longest possible zone first: a.b.com then b.com then com.
 for i in range(0, max(0, len(parts)-1)):
  name='.'.join(parts[i:])
  z=api('/zones?name='+name)
  if z.get('success') and z.get('result'):
   return z['result'][0]
 return None

def ensure_dns_cname(host, tid):
 z=zone_for_hostname(host)
 if not z:
  return {'success':False,'errors':[{'message':'zone not found for '+host}]}
 zid=z['id']; target=tid+'.cfargotunnel.com'
 q=api('/zones/%s/dns_records?type=CNAME&name=%s' % (zid, host))
 body={'type':'CNAME','name':host,'content':target,'proxied':True,'ttl':1}
 if q.get('success') and q.get('result'):
  rid=q['result'][0]['id']
  return api('/zones/%s/dns_records/%s' % (zid,rid),'PUT',body)
 return api('/zones/%s/dns_records' % zid,'POST',body)

def save_ing(d):
 tid=d.get('tunnel_id','').strip(); host=d.get('hostname','').strip(); svc=d.get('service','').strip(); path=d.get('path','').strip(); mode=d.get('mode','edit')
 if not tid or not host or not svc: return {'success':False,'errors':[{'message':'tunnel_id/hostname/service required'}]}
 c=tcfg(tid)
 if not c.get('success'): return c
 res=c.get('result') or {}; conf=res.get('config') or {}; ing=list(conf.get('ingress') or [])
 os.makedirs(BACKUP_DIR,exist_ok=True); open(f'{BACKUP_DIR}/{tid}-{time.strftime("%Y%m%d-%H%M%S")}.json','w').write(json.dumps(res,ensure_ascii=False,indent=2))
 item={'hostname':host,'service':svc};
 if path: item['path']=path
 if mode=='add':
  idx=len(ing); 
  if idx and 'hostname' not in ing[-1]: idx-=1
  ing.insert(idx,item)
 else:
  oh=d.get('old_hostname',''); op=d.get('old_path',''); osvc=d.get('old_service',''); found=False
  for i,it in enumerate(ing):
   if it.get('hostname','')==oh and it.get('path','')==op and (not osvc or it.get('service','')==osvc): ing[i]=item; found=True; break
  if not found: return {'success':False,'errors':[{'message':'old ingress not found'}]}
 conf['ingress']=ing
 u=update(tid,conf)
 if not u.get('success'): return u
 dns=ensure_dns_cname(host,tid)
 # Refresh cache immediately in background so UI sees new hostname.
 refresh_bg()
 notify_3040_sync()
 return {'success':True,'result':{'tunnel_update':u.get('success'), 'dns_update':dns.get('success'), 'dns_errors':dns.get('errors',[])}}

def delete_ing(d):
 tid=d.get('tunnel_id','').strip(); host=d.get('hostname','').strip(); path=d.get('path','').strip(); svc=d.get('service','').strip()
 if not tid or not host: return {'success':False,'errors':[{'message':'tunnel_id and hostname required'}]}
 c=tcfg(tid)
 if not c.get('success'): return c
 res=c.get('result') or {}; conf=res.get('config') or {}; ing=list(conf.get('ingress') or [])
 os.makedirs(BACKUP_DIR,exist_ok=True); open(f'{BACKUP_DIR}/del-{tid}-{time.strftime("%Y%m%d-%H%M%S")}.json','w').write(json.dumps(res,ensure_ascii=False,indent=2))
 new_ing = []
 removed = False
 for it in ing:
  if it.get('hostname','')==host and (not path or it.get('path','')==path) and (not svc or it.get('service','')==svc):
   removed = True
   continue
  new_ing.append(it)
 if not removed: return {'success':False,'errors':[{'message':'ingress not found'}]}
 conf['ingress']=new_ing
 u=update(tid,conf)
 if not u.get('success'): return u
 z=zone_for_hostname(host)
 if z:
  zid=z['id']
  q=api(f'/zones/{zid}/dns_records?type=CNAME&name={host}')
  if q.get('success') and q.get('result'):
   for rec in q['result']:
    api(f'/zones/{zid}/dns_records/{rec["id"]}', method='DELETE')
 refresh_bg()
 notify_3040_sync()
 return {'success':True}

def start_auto_refresh():
 def loop():
  while True:
   time.sleep(60)
   try:
    refresh_bg()
    notify_3040_sync()
   except: pass
 threading.Thread(target=loop,daemon=True).start()

def write_save_status(o):
 os.makedirs(os.path.dirname(SAVE_FILE),exist_ok=True)
 o['time']=time.strftime('%F %T')
 open(SAVE_FILE,'w').write(json.dumps(o,ensure_ascii=False,indent=2))
def read_save_status():
 try: return json.loads(open(SAVE_FILE).read())
 except: return {'running':False,'message':'no save yet'}
def start_save_bg(d):
 if not SAVE_LOCK.acquire(False):
  return {'success':False,'errors':[{'message':'another save is running'}]}
 write_save_status({'running':True,'success':None,'message':'saving','hostname':d.get('hostname','')})
 def work():
  try:
   r=save_ing(d)
   if r.get('success'):
    write_save_status({'running':False,'success':True,'message':'saved','result':r.get('result',{})})
   else:
    write_save_status({'running':False,'success':False,'message':'failed','errors':r.get('errors',[])})
  except Exception as e:
   write_save_status({'running':False,'success':False,'message':'failed','errors':[{'message':str(e)}]})
  finally:
   SAVE_LOCK.release()
 threading.Thread(target=work,daemon=True).start()
 return {'success':True,'accepted':True,'message':'save started'}

HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tunnel</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b1020;color:#e5e7eb;margin:0}.wrap{max-width:1320px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.leftTop{display:flex;align-items:center;gap:14px}.apiMini{position:relative}.apiMini summary{list-style:none;background:#1e293b;border-radius:10px;padding:8px 12px;cursor:pointer;font-weight:700}.apiMini[open] summary{background:#38bdf8;color:#06121f}.apiBox{position:absolute;top:42px;left:0;z-index:10;width:360px;background:#111827;border:1px solid #334155;border-radius:14px;padding:14px;box-shadow:0 18px 40px #0008}.muted{color:#94a3b8}.card,.tile{background:#111827;border:1px solid #263244;border-radius:16px;padding:14px;margin:12px 0}.summaryBar{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:12px;margin:18px 0 12px}.stat{background:#111827;border:1px solid #263244;border-radius:16px;padding:14px 18px;min-height:76px;display:flex;flex-direction:column;justify-content:center}.stat .label{font-size:14px;color:#94a3b8;font-weight:700;margin-bottom:6px}.stat .value{font-size:30px;line-height:1;font-weight:900}.stat.time .value{font-size:15px;line-height:1.2;color:#e5e7eb}.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}.num{font-size:28px;font-weight:800}.ok{color:#86efac}.bad{color:#fca5a5}.warn{color:#fde68a}.pill{display:inline-block;padding:3px 9px;border-radius:999px;background:#1e293b;font-size:12px}.pill.ok{background:#064e3b}.pill.bad{background:#7f1d1d}.pill.warn{background:#713f12}button{background:#38bdf8;border:0;border-radius:10px;padding:8px 12px;font-weight:700;cursor:pointer}.ghost{background:#1e293b;color:#cbd5e1}.tabs{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 20px}.tab{background:#1e293b;color:#cbd5e1;border-radius:14px;padding:11px 16px;min-width:126px;display:flex;align-items:center;justify-content:space-between;gap:12px}.tab.active{background:#38bdf8;color:#06121f}.tab .pill{background:#0f172a55;color:inherit;font-weight:900}.tile.ok{border-color:#166534}.tile.bad{border-color:#991b1b}.tile.warn{border-color:#854d0e}.host{font-weight:800;color:#bae6fd;word-break:break-all}.host a,.svc a{color:inherit;text-decoration:none}.host a:hover,.svc a:hover{text-decoration:underline}.svc{font-family:monospace;color:#c4b5fd;font-size:12px;word-break:break-all}.small{font-size:12px}.tileTop{display:flex;justify-content:space-between;gap:8px}.modal{position:fixed;inset:0;background:#0008;display:none;align-items:center;justify-content:center}.box{background:#111827;border:1px solid #334155;border-radius:16px;padding:18px;max-width:620px;width:92%}input,select{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #334155;background:#020617;color:#e5e7eb;margin:5px 0}@media(max-width:900px){.summaryBar{grid-template-columns:repeat(2,1fr)}.tab{min-width:110px}}@media(max-width:560px){.summaryBar{grid-template-columns:1fr}.tab{min-width:100%}}</style></head><body><div class="wrap"><div class="top"><div class="leftTop"><details class="apiMini"><summary>API</summary><div class="apiBox"><form id="cfg" onsubmit="saveCfg(event)"><input name="account_id" placeholder="CF_ACCOUNT_ID"><input name="token" type="password" placeholder="CF_API_TOKEN"><button>保存</button> <span id="saveMsg"></span></form><p id="api"></p></div></details><h1>Cloudflare Tunnel 总览</h1></div><div><button onclick="openAdd()">新增 Hostname</button> <button onclick="forceRefresh()">强制同步 Cloudflare</button> <button onclick="loadAll()">刷新</button></div></div><div id="stats" class="summaryBar"></div><div id="tabs" class="tabs"></div><div id="content"></div></div><div class="modal" id="modal"><div class="box"><h2 id="mTitle">编辑</h2><form id="editForm" onsubmit="saveIngress(event)"><input type="hidden" name="mode"><input type="hidden" name="old_hostname"><input type="hidden" name="old_path"><input type="hidden" name="old_service"><label>Tunnel</label><select name="tunnel_id" id="tunnelSelect"></select><label>Hostname</label><input name="hostname"><label>Path 可选</label><input name="path"><label>内网 Service</label><input name="service" value="http://"><p class="muted small">保存前自动备份旧配置；支持实时双向同步与一键删除下线。</p><button>保存</button> <button type="button" id="btnDelete" class="bad ghost" style="display:none;color:#f87171;border:1px solid #7f1d1d;margin-left:8px;" onclick="deleteIngress()">删除此 Hostname</button> <button type="button" class="ghost" onclick="closeModal()">取消</button> <span id="editMsg"></span></form></div></div><script>
let ALL=[],GROUPS={},ACTIVE='全部',TUNNELS=[]; async function get(p){let r=await fetch(p);return await r.json()} function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))} function root(h){let a=(h||'无域名').split('.');return a.length>=2?a.slice(-2).join('.'):(h||'无域名')} function publicUrl(h){return h?'https://'+h:''} function serviceUrl(s){return (s||'').startsWith('http://')||(s||'').startsWith('https://')?s:''} function state(r){if(r.tunnelStatus!=='healthy')return'bad';if(r.health&&r.health.checked&&!r.health.ok)return'bad';if(r.health&&r.health.checked&&r.health.ok)return'ok';return'warn'} function htxt(h){h=h||{};if(h.checked)return h.ok?`<span class="pill ok">${esc(h.status||'OK')} · ${esc(h.ms||'')}ms</span>`:`<span class="pill bad">不可达 · ${esc(h.ms||'')}ms</span>`;return `<span class="pill warn">${esc(h.note||'待检查')}</span>`} function tile(r){let pu=publicUrl(r.hostname), su=serviceUrl(r.service);let h=pu?`<a href="${esc(pu)}" target="_blank">${esc(r.hostname)}</a>`:esc(r.hostname);let sv=su?`<a href="${esc(su)}" target="_blank">${esc(r.service)}</a>`:esc(r.service);return `<div class="tile ${state(r)}"><div class="tileTop"><div class="host">${h}</div>${htxt(r.health)}</div><p class="svc">${sv}</p><div class="small muted">Tunnel：${esc(r.tunnel)} · ${esc(r.tunnelStatus)}</div>${r.health&&r.health.error?`<div class="small bad">${esc(r.health.error).slice(0,90)}</div>`:''}<p><button class="ghost" onclick='openEdit(${JSON.stringify(r)})'>编辑</button></p></div>`}
function tabs(){let ns=['全部',...Object.keys(GROUPS).sort()];document.getElementById('tabs').innerHTML=ns.map(n=>`<button class="tab ${n===ACTIVE?'active':''}" onclick="ACTIVE='${esc(n)}';render()"><span>${esc(n)}</span><span class="pill">${n==='全部'?ALL.length:GROUPS[n].length}</span></button>`).join('')} function sec(t,rs){return rs.length?`<div class="card"><h2>${t} <span class="pill">${rs.length}</span></h2><div class="tiles">${rs.map(tile).join('')}</div></div>`:''} function render(){tabs();let rows=ACTIVE==='全部'?ALL:(GROUPS[ACTIVE]||[]);document.getElementById('content').innerHTML=sec('异常 / 需要处理',rows.filter(r=>state(r)==='bad'))+sec('正常',rows.filter(r=>state(r)==='ok'))+sec('待检查 / 非 HTTP',rows.filter(r=>state(r)==='warn'))}
function apply(t){ALL=[];GROUPS={};TUNNELS=[];let seen={};for(let r of (t.result||[])){ALL.push(r);(GROUPS[root(r.hostname)]??=[]).push(r);if(r.tunnel_id&&!seen[r.tunnel_id]){seen[r.tunnel_id]=1;TUNNELS.push({id:r.tunnel_id,name:r.tunnel,status:r.tunnelStatus})}}let bad=ALL.filter(r=>state(r)==='bad').length, ok=ALL.filter(r=>state(r)==='ok').length;document.getElementById('stats').innerHTML=`<div class="stat time"><div class="label">上次检查</div><div class="value">${esc(t.checked_at||'')}</div>${t.running?'<span class="pill warn">正在检查</span>':''}</div><div class="stat"><div class="label">异常</div><div class="value bad">${bad}</div></div><div class="stat"><div class="label">正常</div><div class="value ok">${ok}</div></div><div class="stat"><div class="label">总数</div><div class="value">${ALL.length}</div></div>`;render()} async function forceRefresh(){document.getElementById('content').innerHTML='<div class="card warn">正在从 Cloudflare 强制同步...</div>';let t=await get('/api/force-refresh');if(t.success)apply(t);else document.getElementById('content').innerHTML='<div class="card bad">'+esc((t.errors||[]).map(e=>e.message).join('; ')||JSON.stringify(t))+'</div>'}
async function loadAll(){let st=await get('/api/local');document.getElementById('api').innerHTML=st.api_configured?'<span class="ok">API 已配置</span>':'<span class="bad">API 未配置</span>';let t=await get('/api/cache');if(!t.success){document.getElementById('content').innerHTML='<div class="card bad">'+esc((t.errors||[]).map(e=>e.message).join('; '))+'</div>';return}apply(t);let old=t.checked_at;fetch('/api/refresh');for(let i=0;i<30;i++){await new Promise(r=>setTimeout(r,2000));let n=await get('/api/cache');if(n.checked_at!==old&&!n.running){apply(n);break}}}
function fill(id){document.getElementById('tunnelSelect').innerHTML=TUNNELS.map(t=>`<option value="${esc(t.id)}" ${t.id===id?'selected':''}>${esc(t.name)}</option>`).join('')} function openAdd(){let f=document.getElementById('editForm');f.reset();f.mode.value='add';fill(TUNNELS[0]?.id);document.getElementById('btnDelete').style.display='none';document.getElementById('modal').style.display='flex'} function openEdit(r){let f=document.getElementById('editForm');f.reset();f.mode.value='edit';f.old_hostname.value=r.hostname;f.old_path.value=r.path||'';f.old_service.value=r.service;f.hostname.value=r.hostname;f.path.value=r.path||'';f.service.value=r.service;fill(r.tunnel_id);document.getElementById('btnDelete').style.display='inline-block';document.getElementById('modal').style.display='flex'} function closeModal(){document.getElementById('modal').style.display='none'} async function saveIngress(e){e.preventDefault();let msg=document.getElementById('editMsg');msg.textContent='已提交，后台保存中...';let body=new URLSearchParams(new FormData(document.getElementById('editForm')));let j=await (await fetch('/api/ingress/save',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body})).json();if(!j.success){msg.textContent='提交失败：'+((j.errors||[]).map(x=>x.message).join('; ')||JSON.stringify(j));return} for(let i=0;i<45;i++){await new Promise(r=>setTimeout(r,1000));let st=await get('/api/save-status');if(!st.running){if(st.success){let dns=st.result&&st.result.dns_update?'DNS 已同步':'DNS 未同步';msg.textContent='保存成功，'+dns+'，正在刷新...';setTimeout(()=>{closeModal();forceRefresh()},600)}else{msg.textContent='保存失败：'+((st.errors||[]).map(x=>x.message).join('; ')||JSON.stringify(st))}return}} msg.textContent='仍在后台保存，可先关闭后点强制同步';} async function deleteIngress(){if(!confirm('确定要删除此 Hostname 并在 Cloudflare 和穿透优选中心中下线吗？此操作不可逆！')) return;let msg=document.getElementById('editMsg');msg.textContent='正在删除中...';let body=new URLSearchParams(new FormData(document.getElementById('editForm')));try{let j=await (await fetch('/api/ingress/delete',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body})).json();if(j.success){msg.textContent='删除成功，正在刷新...';setTimeout(()=>{closeModal();forceRefresh()},600);}else{msg.textContent='删除失败：'+((j.errors||[]).map(x=>x.message).join('; ')||JSON.stringify(j));}}catch(e){msg.textContent='请求失败：'+e;}} async function saveCfg(e){e.preventDefault();let body=new URLSearchParams(new FormData(document.getElementById('cfg')));let j=await (await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body})).json();document.getElementById('saveMsg').textContent=j.success?'已保存':'失败';setTimeout(loadAll,800)} loadAll();
</script></body></html>'''
class H(BaseHTTPRequestHandler):
 def js(self,o,code=200):
  b=json.dumps(o,ensure_ascii=False,indent=2).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/':
   b=HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if path=='/api/local': return self.js({'hostname':run('hostname'),'ip':run("hostname -I|awk '{print $1}'"),'api_configured':bool(CONFIG.get('CF_API_TOKEN') and CONFIG.get('CF_ACCOUNT_ID'))})
  if path=='/api/cache': return self.js(read_cache())
  if path=='/api/refresh': return self.js({'success':True,'started':refresh_bg()})
  if path=='/api/force-refresh': return self.js(force_refresh_now())
  if path=='/api/summary': return self.js(rows(False))
  if path=='/api/save-status': return self.js(read_save_status())
  self.js({'success':False,'errors':[{'message':'not found'}]},404)
 def do_POST(self):
  path=urlparse(self.path).path; n=int(self.headers.get('Content-Length','0')); d={k:v[0] for k,v in parse_qs(self.rfile.read(n).decode()).items()}
  global CONFIG
  try:
   if path=='/api/config': save_env(d.get('token',''),d.get('account_id','')); CONFIG=env(); return self.js({'success':True})
   if path=='/api/ingress/save': return self.js(start_save_bg(d))
   if path=='/api/ingress/delete': return self.js(delete_ing(d))
   self.js({'success':False,'errors':[{'message':'not found'}]},404)
  except Exception as e: self.js({'success':False,'errors':[{'message':str(e)}]},500)
 def log_message(self,*a): return
if __name__=='__main__':
 start_auto_refresh()
 print(f'tunnel-panel listening on {HOST}:{PORT}',flush=True)
 ThreadingHTTPServer((HOST,PORT),H).serve_forever()
