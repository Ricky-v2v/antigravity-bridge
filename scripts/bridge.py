import json,asyncio,websockets,urllib.request,time,threading,re,argparse,uuid
from http.server import HTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from collections import OrderedDict
from socketserver import ThreadingMixIn

MODELS=['Gemini 3.1 Pro (High)','Gemini 3.1 Pro (Low)','Gemini 3 Flash','Claude Sonnet 4.6 (Thinking)','Claude Opus 4.6 (Thinking)','GPT-OSS 120B (Medium)']
PREFIX=''
RELOAD_TIMEOUT=15
VERSION='v17-fast'

# ─── Async task store ───
_tasks=OrderedDict();_tlock=threading.Lock();_TMAX=50
def _tadd(tid,kind):
    with _tlock:
        if len(_tasks)>=_TMAX:_tasks.popitem(last=False)
        _tasks[tid]={'id':tid,'kind':kind,'status':'running','result':None,'error':None,'created':time.time()}
def _tdone(tid,r):
    with _tlock:
        if tid in _tasks:_tasks[tid]['status']='ok';_tasks[tid]['result']=r
def _tfail(tid,e):
    with _tlock:
        if tid in _tasks:_tasks[tid]['status']='error';_tasks[tid]['error']=str(e)
def _tget(tid):
    with _tlock:return _tasks.get(tid)

# ─── Persistent event loop (single background thread) ───
class _AsyncLoop:
    def __init__(s):
        s.loop=asyncio.new_event_loop()
        s._t=threading.Thread(target=s._run,daemon=True);s._t.start()
    def _run(s):asyncio.set_event_loop(s.loop);s.loop.run_forever()
    def submit(s,coro,timeout=None):
        return asyncio.run_coroutine_threadsafe(coro,s.loop).result(timeout=timeout)

class Bridge:
    def __init__(s,cdp=9229):
        s.cdp=cdp;s.lock=threading.Lock()
        s.model='Claude Opus 4.6 (Thinking)';s.mc=0
        s._al=_AsyncLoop()
        s._ws=None;s._mid=0;s._pending={};s._recv=None;s._obs_ok=False

    # ─── WebSocket URL discovery (sync, called from async via executor) ───
    def _find_ws(s):
        t=json.loads(urllib.request.urlopen(f'http://127.0.0.1:{s.cdp}/json/list',timeout=5).read())
        a=[x for x in t if 'workbench.html' in x.get('url','') and 'jetski' not in x.get('url','')]
        if not a:a=[x for x in t if x.get('title') in ('Antigravity','Task')]
        if not a:a=[x for x in t if 'workbench' in x.get('url','')]
        if not a:raise Exception('No Antigravity')
        return a[0]['webSocketDebuggerUrl']

    # ─── Persistent connection ───
    async def _conn(s):
        """Ensure WebSocket is connected; reuse existing or create new."""
        if s._ws:
            try:
                st=getattr(s._ws,'state',None)
                if st is not None:
                    try:
                        from websockets.protocol import State
                        if st==State.OPEN:return
                    except Exception:
                        if int(st)==1:return
                if getattr(s._ws,'open',False):return
            except Exception:
                pass
        url=await asyncio.get_event_loop().run_in_executor(None,s._find_ws)
        s._ws=await websockets.connect(url,max_size=10*1024*1024,open_timeout=10)
        s._pending={};s._obs_ok=False
        if s._recv:s._recv.cancel()
        s._recv=asyncio.ensure_future(s._rx())
        await s._ssl()

    async def _rx(s):
        """Background receiver: dispatch CDP responses by message id."""
        try:
            async for raw in s._ws:
                m=json.loads(raw)
                mid=m.get('id')
                if mid and mid in s._pending:
                    if not s._pending[mid].done(): s._pending[mid].set_result(m)
        except Exception:pass
        finally:
            s._ws=None
            for fut in s._pending.values():
                if not fut.done(): fut.set_exception(Exception("WS Closed"))
            s._pending.clear()

    async def _ev(s,js,timeout=30):
        """Evaluate JS via CDP, return value."""
        await s._conn()
        s._mid+=1;mid=s._mid
        fut=s._al.loop.create_future();s._pending[mid]=fut
        try:
            await s._ws.send(json.dumps({'id':mid,'method':'Runtime.evaluate',
                'params':{'expression':js,'returnByValue':True,'awaitPromise':True}}))
            r=await asyncio.wait_for(fut,timeout=timeout)
            v=r.get('result',{}).get('result',{})
            return v.get('value',v.get('description',''))
        finally:s._pending.pop(mid,None)

    async def _cdp(s,method,params=None,timeout=5):
        """Raw CDP method call."""
        await s._conn()
        s._mid+=1;mid=s._mid
        fut=s._al.loop.create_future();s._pending[mid]=fut
        try:
            await s._ws.send(json.dumps({'id':mid,'method':method,'params':params or{}}))
            return await asyncio.wait_for(fut,timeout=timeout)
        finally:s._pending.pop(mid,None)

    async def _ssl(s):
        for m in['Security.enable','Security.setIgnoreCertificateErrors']:
            try:await s._cdp(m,{'ignore':True} if 'Ignore' in m else{},timeout=3)
            except:pass

    # ─── Page readiness ───
    async def _wait_ready(s,timeout=20):
        t0=time.time()
        while time.time()-t0<timeout:
            try:
                await s._ev("""(()=>{
                    const btns=[...document.querySelectorAll('button')];
                    const a=btns.find(b=>b.textContent.trim()==='Allow This Conversation')||btns.find(b=>b.textContent.trim()==='Allow Once');
                    if(a)a.click();
                })()""")
                r=await s._ev("!!document.querySelector('div[contenteditable=\"true\"]')")
                if str(r)=='True':await asyncio.sleep(0.3);return True
            except:pass
            await asyncio.sleep(0.3)
        return False

    # ─── MutationObserver injection (KEY OPTIMIZATION) ───
    async def _inject_obs(s):
        """Inject or reset MutationObserver for O(1) completion detection.
        
        Instead of polling document.body.innerText (O(page_size)) every 2s,
        we inject a MutationObserver that watches for new response controls.
        The observer sets window.__ag.done=true when a NEW response completes.
        Our polling loop just checks this boolean — near-zero overhead.
        """
        if s._obs_ok:
            r = await s._ev("""(()=>{
                if(!window.__ag) return 'MISS';
                window.__ag.done=false;
                const btns=[...document.querySelectorAll('button')];
                window.__ag.good=btns.filter(b=>b.textContent.trim()==='Good').length;
                window.__ag.copy=btns.filter(b=>b.textContent.trim()==='Copy').length;
                return 'OK';
            })()""")
            if str(r) == 'OK': return
            s._obs_ok = False # 如果丢失了，继续往下重新注入
        await s._ev("""(()=>{
            const btns=[...document.querySelectorAll('button')];
            window.__ag={
                done:false,
                good:btns.filter(b=>b.textContent.trim()==='Good').length,
                copy:btns.filter(b=>b.textContent.trim()==='Copy').length
            };
            let tm=null;
            const ck=()=>{
                if(window.__ag.done)return;
                const curBtns=[...document.querySelectorAll('button')];
                const curGood=curBtns.filter(b=>b.textContent.trim()==='Good').length;
                const curCopy=curBtns.filter(b=>b.textContent.trim()==='Copy').length;
                if(curGood>window.__ag.good || curCopy>window.__ag.copy)window.__ag.done=true;
            };
            if(window.__agObs)window.__agObs.disconnect();
            window.__agObs=new MutationObserver(()=>{
                if(window.__ag.done)return;
                if(tm)clearTimeout(tm);
                tm=setTimeout(ck,80);
            });
            window.__agObs.observe(document.body,{childList:true,subtree:true});
        })()""")
        s._obs_ok=True

    # ─── Fast mode ───
    async def _fast_mode(s):
        cur=await s._ev("""(()=>{
            const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Fast'||b.textContent.trim()==='Planning');
            return b?b.textContent.trim():'';
        })()""")
        if str(cur)=='Fast':return True
        await s._ev("""(()=>{
            const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Planning'||b.textContent.trim()==='Fast');
            if(b)b.click();
        })()""")
        await asyncio.sleep(0.15)
        r=await s._ev("""(()=>{
            for(const e of document.querySelectorAll('*')){
                if(e.textContent.trim()==='Fast'&&e.childElementCount===0){e.click();return'OK';}
            }return'NO';
        })()""")
        await asyncio.sleep(0.15)
        return 'OK' in str(r)

    # ─── Model switch ───
    async def _set_model(s,model):
        if not model or model==s.model:return
        await s._ev(r"""(()=>{const ss=document.querySelectorAll('span');for(const x of ss){if(x.className.includes('select-none')&&x.className.includes('min-w-0')){const p=x.parentElement;if(p){p.click();return'OK'}}}return'NO'})()""")
        await asyncio.sleep(0.4)
        safe=model.replace("'","\\'")
        r=await s._ev(f"""(()=>{{const a=document.querySelectorAll('*');for(const e of a){{if(e.childElementCount===0&&e.textContent.trim()==='{safe}'){{let t=e;for(let i=0;i<5;i++){{const c=(t.className||'');if(c.includes('cursor-pointer')||c.includes('hover:')||c.includes('px-2')){{t.click();return'OK'}}t=t.parentElement;if(!t)break}}e.click();return'OK'}}}}return'NO'}})()""")
        await asyncio.sleep(0.2)
        if 'OK' in str(r):s.model=model

    # ─── Send prompt ───
    async def _type_send(s,prompt):
        full=PREFIX+prompt
        safe=full.replace('\\','\\\\').replace("'","\\'").replace('\n','\\n').replace('\r','')
        r=await s._ev(f"""(()=>{{const d=document.querySelector('div[role="textbox"][contenteditable="true"]')||document.querySelector('[data-lexical-editor="true"]');if(!d)return'NO';d.focus();document.execCommand('selectAll');document.execCommand('delete');document.execCommand('insertText',false,'{safe}');return'OK'}})()""")
        if str(r)!='OK':return f'type:{r}'
        await asyncio.sleep(0.15)
        r=await s._ev("""(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Send');if(b&&!b.disabled){b.click();return'OK';}return'NO';})()""")
        if 'OK' not in str(r):
            await s._cdp('Input.dispatchKeyEvent',{'type':'keyDown','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,'nativeVirtualKeyCode':13})
            await s._cdp('Input.dispatchKeyEvent',{'type':'keyUp','key':'Enter','code':'Enter','windowsVirtualKeyCode':13,'nativeVirtualKeyCode':13})
        return 'OK'

    # ─── Poll completion (adaptive interval + observer flag) ───
    async def _poll(s,prompt,timeout):
        """Wait for AI response. Uses observer flag (O(1)) with body-text fallback."""
        t0=time.time();marker=prompt[:60];last_fb=0
        while time.time()-t0<timeout:
            el=time.time()-t0
            # Adaptive: fast early, slower later
            iv=0.2 if el<5 else(0.4 if el<30 else(0.8 if el<120 else 1.5))
            await asyncio.sleep(iv)
            # Fast path: check observer boolean (O(1), no layout reflow)
            try:
                done=await s._ev('window.__ag&&window.__ag.done',timeout=5)
                if str(done) in('true','True'):
                    body=str(await s._ev('document.body.innerText'))
                    parts=body.split(marker)
                    after=parts[-1] if len(parts)>=2 else body
                    if 'high traffic' in after:
                        return{'response':'Server high traffic','elapsed':round(time.time()-t0,1),'model':s.model,'status':'high_traffic'}
                    if 'Agent execution terminated' in after or 'Agent terminated' in after:
                        return{'error':'agent_error','status':'agent_error','elapsed':round(time.time()-t0,1)}
                    return{'response':s._clean(after,prompt),'elapsed':round(time.time()-t0,1),'model':s.model,'status':'ok'}
            except:pass
            # Fallback: full body check every 15s in case observer failed
            if el-last_fb>15:
                last_fb=el
                try:
                    body=str(await s._ev('document.body.innerText'))
                    parts=body.split(marker)
                    if len(parts)>=2:
                        after=parts[-1]
                        has_done=('Good\nBad' in after) or ('\nCopy' in after) or (after.strip().endswith('Copy'))
                        has_gen='Generating' in after
                        if has_done and not has_gen:
                            return{'response':s._clean(after,prompt),'elapsed':round(time.time()-t0,1),'model':s.model,'status':'ok'}
                        if has_done and 'high traffic' in after:
                            return{'response':'Server high traffic','elapsed':round(time.time()-t0,1),'model':s.model,'status':'high_traffic'}
                except:pass
        # Timeout
        try:
            body=str(await s._ev('document.body.innerText'))
            parts=body.split(marker);after=parts[-1] if len(parts)>=2 else body
            return{'response':s._clean(after,prompt),'elapsed':round(time.time()-t0,1),'model':s.model,'status':'timeout'}
        except:
            return{'error':'timeout+read_fail','status':'error','elapsed':round(time.time()-t0,1)}

    # ─── Core chat ───
    async def _do_chat(s,prompt,timeout,model):
        await s._conn()
        if not await s._wait_ready(timeout=10):
            return{'error':'page not ready','status':'error'}
        await s._fast_mode()
        await s._set_model(model)
        s.mc+=1
        if s.mc>10:
            await s._reload_page();s.mc=1
            return await s._do_chat(prompt,timeout,model)
        await s._inject_obs()
        r=await s._type_send(prompt)
        if r!='OK':return{'error':r,'status':'error'}
        return await s._poll(prompt,timeout)

    async def _chat(s,prompt,timeout,model):
        for att in range(3):
            try:
                r=await s._do_chat(prompt,timeout,model)
                if r.get('status') in('ok','high_traffic','timeout','agent_error'):return r
                if att<2:
                    try:await s._reload_page()
                    except:pass
                    continue
                return r
            except Exception as e:
                if att<2:s._ws=None;await asyncio.sleep(1);continue
                return{'error':str(e),'status':'error'}
        return{'error':'max retries','status':'error'}

    # ─── Reload ───
    async def _reload_page(s):
        try:
            await s._conn()
            try:await s._ev('location.reload()')
            except:pass
        except:pass
        if s._ws:
            try:await s._ws.close()
            except:pass
        s._ws=None;s._obs_ok=False
        await asyncio.sleep(2)
        for _ in range(5):
            try:
                await s._conn()
                ok=await s._wait_ready(timeout=RELOAD_TIMEOUT)
                s.mc=0;return{'status':'ok','method':'reload','ready':ok}
            except:s._ws=None;await asyncio.sleep(1.5)
        return{'status':'error','error':'reload reconnect failed'}

    # ─── Switch model (standalone API) ───
    async def _sw(s,name):
        await s._conn()
        await s._set_model(name)
        return{'status':'ok','model':name}

    # ─── Image helpers ───
    async def _get_img_count(s):
        await s._conn()
        n=await s._ev('document.querySelectorAll(\'img[alt="Generated image preview"]\').length')
        return{'count':int(str(n) or '0')}

    async def _extract_image(s,after_count=0):
        t0=time.time()
        while time.time()-t0<60:
            await s._conn()
            count=await s._ev('document.querySelectorAll(\'img[alt="Generated image preview"]\').length')
            if int(str(count) or '0')>after_count:
                b64=await s._ev('''(async()=>{
                    const imgs=document.querySelectorAll('img[alt="Generated image preview"]');
                    const img=imgs[imgs.length-1];if(!img)return'';
                    try{const resp=await fetch(img.src);const blob=await resp.blob();
                    return new Promise(r=>{const rd=new FileReader();rd.onload=()=>r(rd.result.split(",")[1]);rd.readAsDataURL(blob);});}
                    catch(e){return'ERR:'+e.message;}
                })()''')
                if b64 and not str(b64).startswith('ERR'):
                    return{'status':'ok','image':b64,'count':count}
            await asyncio.sleep(2)
        return{'status':'error','error':'extract timeout'}

    async def _get_history(s):
        await s._conn()
        body=str(await s._ev('document.body.innerText'))
        return{'status':'ok','content':s._clean(body),'raw_length':len(body)}

    # ─── Public sync API (called from HTTP threads) ───
    def chat(s,p,to=300,m=None):
        with s.lock:return s._al.submit(s._chat(p,to,m),timeout=to+30)
    def switch(s,m):
        with s.lock:return s._al.submit(s._sw(m))
    def new_chat(s):
        with s.lock:return s._al.submit(s._reload_page())

    # ─── Text cleanup ───
    def _clean(s,after,prompt=''):
        raw=after
        for f in['\nAsk anything','\nPlanning\n','\nSend\n','\nSend']:
            i=raw.rfind(f)
            if i>0:raw=raw[:i];break
        for m in MODELS:raw=raw.replace('\n'+m,'')
        for m in['\nModel','\nNew']:raw=raw.replace(m,'')
        raw=re.sub(r'^Thought for [<\d]+s\n?','',raw,flags=re.MULTILINE)
        raw=re.sub(
            r'^(Planning|Executing|Verifying|Looking for|Reading|Writing|Creating|Editing|'
            r'Viewing|Searching|Researching|Defining|Formulating|Considering|Analyzing|'
            r'Processing|Initiating|Calculating|Refining|Delivering|Determining|'
            r'Identifying|Evaluating|Preparing|Checking)[\s:].*$',
            '',raw,flags=re.MULTILINE
        )
        for n in['CRITICAL INSTRUCTION 1:','CRITICAL INSTRUCTION 2:']:
            i=raw.find(n)
            if i>=0:
                j=raw.find('\n\n',i)
                raw=raw[:i]+(raw[j+2:] if j>=0 else '')
        raw=re.sub(r'\nGood\nBad\s*','',raw)
        raw=re.sub(r'\n{3,}','\n\n',raw)
        for n in['Agent terminated','See our troubleshooting','Dismiss\nCopy debug','Error\nOur servers','Error\nVerification Required','[Direct mode]']:
            i=raw.find(n)
            if i>=0:raw=raw[:i]
        lines=[l for l in raw.split('\n') if not (l.strip() and len(l.strip().split())<4 and l.strip().endswith('.') and l.strip()[0].isupper())]
        return '\n'.join(lines).strip()

# ─── HTTP Server ───
b=None
class H(BaseHTTPRequestHandler):
    def _read_json(s,allow_empty=False):
        try:ln=int(s.headers.get('Content-Length') or 0)
        except:ln=0
        if ln<=0:
            if allow_empty:return {}
            raise ValueError('empty')
        raw=s.rfile.read(ln)
        try:d=json.loads(raw)
        except Exception as e:raise ValueError('invalid_json') from e
        if not isinstance(d,dict):raise ValueError('invalid_json')
        return d
    def _int(s,v,default):
        if v is None:return default
        if isinstance(v,bool):raise ValueError('invalid_int')
        try:return int(v)
        except Exception as e:raise ValueError('invalid_int') from e
    def do_POST(s):
        try:
            d=s._read_json(allow_empty=(s.path=='/new'))
        except ValueError as e:
            s._j(400,{'error':str(e),'status':'error'})
            return
        if s.path=='/chat':
            p=str(d.get('prompt',''))
            m=d.get('model')
            try:to=s._int(d.get('timeout',180),180)
            except ValueError:
                s._j(400,{'error':'invalid_timeout','status':'error'})
                return
            ts=time.strftime('%H:%M:%S');print(f'[{ts}] >> {p[:80]}',flush=True)
            try:
                r=b.chat(p,to,m);s._j(200,r)
                print(f'[{ts}] << [{r.get("status")}] {r.get("response",r.get("error",""))[:80]} ({r.get("elapsed",0)}s)',flush=True)
            except Exception as e:
                msg=str(e) or type(e).__name__
                s._j(500,{'error':msg,'status':'error'})
                print(f'[{ts}] !! {msg}',flush=True)
        elif s.path=='/model':
            mn=str(d.get('model',''))
            if mn not in MODELS:s._j(400,{'error':'Unknown','status':'error'})
            else:
                try:s._j(200,b.switch(mn))
                except Exception as e:s._j(500,{'error':str(e),'status':'error'})
        elif s.path=='/async':
            p=str(d.get('prompt',''))
            m=d.get('model')
            try:to=s._int(d.get('timeout',600),600)
            except ValueError:
                s._j(400,{'error':'invalid_timeout','status':'error'})
                return
            tid=uuid.uuid4().hex[:12];_tadd(tid,'chat')
            ts=time.strftime('%H:%M:%S');print(f'[{ts}] >> [async:{tid}] {p[:80]}',flush=True)
            def _run():
                try:r=b.chat(p,to,m);_tdone(tid,r)
                except Exception as e:_tfail(tid,e)
            threading.Thread(target=_run,daemon=True).start()
            s._j(200,{'status':'accepted','task_id':tid})
        elif s.path=='/new':
            try:s._j(200,b.new_chat())
            except Exception as e:s._j(500,{'error':str(e),'status':'error'})
        else:s.send_response(404);s.end_headers()
    def do_GET(s):
        if s.path=='/health':
            try:
                t=json.loads(urllib.request.urlopen(f'http://127.0.0.1:{b.cdp}/json/list',timeout=5).read())
                ok=any(x.get('title') in ('Antigravity','Task') or 'workbench.html' in x.get('url','') for x in t)
                s._j(200,{'status':'ok' if ok else 'no_target','model':b.model,'msgs':b.mc,'version':VERSION})
            except:s._j(200,{'status':'cdp_down'})
        elif s.path=='/models':s._j(200,{'models':MODELS,'current':b.model})
        elif s.path=='/imgcount':
            try:s._j(200,b._al.submit(b._get_img_count()))
            except Exception as e:s._j(500,{'error':str(e),'status':'error'})
        elif s.path=='/history':
            try:s._j(200,b._al.submit(b._get_history()))
            except Exception as e:s._j(500,{'error':str(e),'status':'error'})
        elif s.path.startswith('/task/'):
            tid=s.path.split('/task/',1)[1].strip('/')
            t=_tget(tid)
            if not t:s._j(404,{'error':'not found','status':'error'})
            else:s._j(200,t)
        elif s.path.startswith('/extract'):
            qs=parse_qs(urlparse(s.path).query)
            after=int(qs.get('after',['0'])[0])
            try:s._j(200,b._al.submit(b._extract_image(after_count=after)))
            except Exception as e:s._j(500,{'error':str(e),'status':'error'})
        else:s.send_response(404);s.end_headers()
    def _j(s,c,d):s.send_response(c);s.send_header('Content-Type','application/json');s.end_headers();s.wfile.write(json.dumps(d).encode())
    def log_message(s,*a):pass

class ThreadedHTTPServer(ThreadingMixIn,HTTPServer):daemon_threads=True

if __name__=='__main__':
    pa=argparse.ArgumentParser();pa.add_argument('--port',type=int,default=19999);pa.add_argument('--cdp-port',type=int,default=9229)
    a=pa.parse_args();b=Bridge(a.cdp_port)
    print(f'AG Bridge {VERSION} :{a.port}',flush=True)
    ThreadedHTTPServer(('0.0.0.0',a.port),H).serve_forever()
