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
 return {'success':True,'checked_at':time.strftime('%F %T'),'checked_ts':int(time.time()),'running':False,'result':out}
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
  o['checked_at']=time.strftime('%F %T'); o['checked_ts']=int(time.time())
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
UPTIME_FILE='/var/lib/tunnel-panel/uptime.json'; UP_LOCK=threading.Lock(); UP_KEEP=14
def _day(ts=None): return time.strftime('%F',time.gmtime((ts or time.time())+8*3600))
def record_uptime(o):
 """每轮巡检累加当天 [up,total]（北京时间切日）；不探测的非 HTTP 服务不计；失败不吞，记日志。"""
 try:
  day=_day()
  with UP_LOCK:
   try: data=json.loads(open(UPTIME_FILE).read())
   except Exception: data={}
   for r in o.get('result') or []:
    h=r.get('health') or {}
    if r.get('tunnelStatus')=='healthy' and not h.get('checked'): continue
    up = 1 if (r.get('tunnelStatus')=='healthy' and h.get('ok')) else 0
    d=data.setdefault(r['hostname'],{}); c=d.get(day,[0,0]); d[day]=[c[0]+up,c[1]+1]
   keep=sorted({k for d in data.values() for k in d})[-UP_KEEP:]
   for h in list(data):
    data[h]={k:v for k,v in data[h].items() if k in keep}
    if not data[h]: del data[h]
   os.makedirs(os.path.dirname(UPTIME_FILE),exist_ok=True); tmp=UPTIME_FILE+'.tmp'; open(tmp,'w').write(json.dumps(data,ensure_ascii=False)); os.replace(tmp,UPTIME_FILE)
 except Exception as e: print('record_uptime failed:',e,flush=True)
def read_uptime():
 try: data=json.loads(open(UPTIME_FILE).read())
 except Exception: data={}
 days=[_day(time.time()-i*86400) for i in range(6,-1,-1)]
 return {'success':True,'days':days,'data':{h:{k:v for k,v in d.items() if k in days} for h,d in data.items()}}
def refresh_bg():
 if not LOCK.acquire(False): return False
 def work():
  try:
   old=read_cache(); old['running']=True; old['checked_at']=time.strftime('%F %T'); old['checked_ts']=int(time.time()); write_cache(old); new=rows(True); write_cache(new)
   if new.get('success'): record_uptime(new)
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

_D = os.path.dirname(os.path.abspath(__file__))
UI_FILES = [os.path.join(_D, 'ui', 'index.html'), os.path.join(_D, '..', 'app', 'tunnel', 'index.html')]  # 部署=ui/index.html；仓库=app/tunnel/index.html
def load_ui():
 try: return open(next(p for p in UI_FILES if os.path.exists(p)),'rb').read()
 except Exception as e: return ('<!doctype html><meta charset="utf-8"><title>Tunnel</title><p>页面文件读取失败：%s</p>' % e).encode()

class H(BaseHTTPRequestHandler):
 def js(self,o,code=200):
  b=json.dumps(o,ensure_ascii=False,indent=2).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/':
   b=load_ui(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if path=='/api/local': return self.js({'hostname':run('hostname'),'ip':run("hostname -I|awk '{print $1}'"),'api_configured':bool(CONFIG.get('CF_API_TOKEN') and CONFIG.get('CF_ACCOUNT_ID'))})
  if path=='/api/cache': return self.js(read_cache())
  if path=='/api/refresh': return self.js({'success':True,'started':refresh_bg()})
  if path=='/api/force-refresh': return self.js(force_refresh_now())
  if path=='/api/summary': return self.js(rows(False))
  if path=='/api/save-status': return self.js(read_save_status())
  if path=='/api/uptime': return self.js(read_uptime())
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
