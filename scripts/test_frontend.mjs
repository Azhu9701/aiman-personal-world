#!/usr/bin/env node
/**
 * Repeatable DOM interaction and API integration checks for the local UI.
 * Run: node scripts/test_frontend.mjs
 * Requirements: Node.js 20+ and Python 3.10+ (PYTHON may select an executable).
 * Uses only built-in dependencies and examples/demo.json. No private account
 * discovery, external network, browser installation, screenshot, or real data.
 * This small DOM harness does not validate layout, CSS, native media playback,
 * browser accessibility, or native focus behavior; it is not a visual test.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
if(typeof fetch!=='function')throw new Error('This check requires Node.js 20+ with built-in fetch.');
const PYTHON_BRIDGE = String.raw`
import json,os,re,signal,sys
from pathlib import Path
from html.parser import HTMLParser
payload=json.loads(sys.stdin.readline())
root=Path(payload['root']);temporary=Path(payload['temporary']);state=temporary/'state'
sys.path.insert(0,str(root))
import server
class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root={'tag':'document','attrs':{},'children':[]};self.stack=[self.root]
    def handle_starttag(self,tag,attrs):
        node={'tag':tag,'attrs':dict(attrs),'children':[]};self.stack[-1]['children'].append(node)
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:self.stack.append(node)
    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,0,-1):
            if self.stack[index]['tag']==tag:self.stack=self.stack[:index];return
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if self.stack[-1].get('tag')==tag:self.stack.pop()
    def handle_data(self,data):
        if self.stack[-1]['tag'] not in {'script','style'}:self.stack[-1]['children'].append({'text':data})
for name in ('world','index'):
    html=(root/(name+'.html')).read_text(encoding='utf-8');parser=Tree();parser.feed(html)
    files={name+'-tree.json':json.dumps(parser.root,ensure_ascii=False),name+'-script.js':'\n'.join(re.findall(r'<script>(.*?)</script>',html,re.S))}
    for name,value in files.items():
        file=temporary/name;file.write_text(value,encoding='utf-8');file.chmod(0o600)
server.import_archive(root/'examples/demo.json',state)
app=server.ViewerServer(0,state,payload['token'])
def terminate(*_):raise SystemExit(0)
signal.signal(signal.SIGTERM,terminate)
try:
    print(json.dumps({'ready':True,'port':app.server_address[1],'cookie_name':app.cookie_name}),flush=True)
    app.serve_forever()
finally:app.server_close()
`;

async function startService() {
  const tempRoot=fs.mkdtempSync(path.join(os.tmpdir(),'aiman-world-ui-'));
  fs.chmodSync(tempRoot,0o700);
  const token=crypto.randomBytes(24).toString('hex');
  const executable=process.env.PYTHON || (process.platform==='win32'?'python':'python3');
  const child=spawn(executable,['-u','-c',PYTHON_BRIDGE],{cwd:ROOT,stdio:['pipe','pipe','pipe']});
  let stopped=false, stderr='';
  child.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString()).slice(-6000);});
  async function stop() {
    if(stopped)return;stopped=true;
    if(child.exitCode===null&&child.signalCode===null){
      child.kill('SIGTERM');
      await new Promise(resolve=>{const timer=setTimeout(()=>{child.kill('SIGKILL');resolve();},3000);child.once('close',()=>{clearTimeout(timer);resolve();});});
    }
    fs.rmSync(tempRoot,{recursive:true,force:true});
  }
  try {
    const ready=await new Promise((resolve,reject)=>{
      let buffer='';const timer=setTimeout(()=>reject(new Error('Temporary synthetic service did not start within 15 seconds.')),15000);
      child.once('error',error=>{clearTimeout(timer);reject(new Error('Could not start Python for the synthetic test service: '+error.code));});
      child.once('exit',code=>{clearTimeout(timer);reject(new Error('Synthetic service exited before startup: '+code+'\n'+stderr));});
      child.stdout.on('data',chunk=>{buffer+=chunk.toString();const end=buffer.indexOf('\n');if(end<0)return;try{const value=JSON.parse(buffer.slice(0,end));if(value.ready){clearTimeout(timer);resolve(value);}else{clearTimeout(timer);reject(new Error('Unexpected synthetic service response.'));}}catch(_){clearTimeout(timer);reject(new Error('Synthetic service startup response was not JSON.'));}});
      // Private in-process IPC; the random token is never written to logs or CLI arguments.
      child.stdin.end(JSON.stringify({root:ROOT,temporary:tempRoot,token})+'\n');
    });
    const fixture=JSON.parse(fs.readFileSync(path.join(ROOT,'examples/demo.json'),'utf8'));
    assert.strictEqual(fixture.manifest.synthetic,true,'The UI fixture must be explicitly synthetic.');
    const first=fixture.messages[0];assert(first,'Synthetic fixture must contain messages.');
    return {tempRoot,stop,config:{base:'http://127.0.0.1:'+ready.port,cookie:ready.cookie_name+'='+token,cid:first.conversation_id,mid:first.id,messages:fixture.messages.length}};
  } catch(error) { await stop();throw error; }
}
const environment=await startService();
const config=environment.config;
const pending = new Set(), errors = [], requests = [];
const handlers = {};
const uiTimers = new Set();
function later(fn, delay=0) { const id=setTimeout(()=>{uiTimers.delete(id);fn();},delay);uiTimers.add(id);return id; }
function cancelLater(id) { clearTimeout(id);uiTimers.delete(id); }
let modify = null;
class Classes {
  constructor(node) { this.node = node; }
  values() { return new Set((this.node.className || '').split(/\s+/).filter(Boolean)); }
  contains(s) { return this.values().has(s); }
  add(...items) { const values = this.values(); items.forEach(x => values.add(x)); this.node.className = [...values].join(' '); }
  remove(...items) { const values = this.values(); items.forEach(x => values.delete(x)); this.node.className = [...values].join(' '); }
  toggle(value, force) { const active = force === undefined ? !this.contains(value) : !!force; active ? this.add(value) : this.remove(value); return active; }
}
function matches(node, selector) {
  if (node.nodeType !== 1) return false;
  const tag = selector.match(/^[A-Za-z][\w-]*/);
  if (tag && node.tagName.toLowerCase() !== tag[0].toLowerCase()) return false;
  for (const cls of selector.matchAll(/\.([\w-]+)/g)) if (!node.classList.contains(cls[1])) return false;
  const id = selector.match(/#([\w-]+)/); if (id && node.id !== id[1]) return false;
  for (const attribute of selector.matchAll(/\[([\w-]+)(?:=["']?([^"'\]]+)["']?)?\]/g)) {
    const value = node.getAttribute(attribute[1]);
    if (value === null || (attribute[2] !== undefined && value !== attribute[2])) return false;
  }
  return true;
}
class Node {
  constructor(tag = '', text = undefined) {
    this.nodeType = text === undefined ? 1 : 3;
    this.tagName = tag.toUpperCase(); this._text = text || ''; this.parentNode = null; this.childNodes = [];
    this.attrs = {}; this.dataset = {}; this.events = {}; this.className = ''; this.classList = new Classes(this);
    this.style = {setProperty(k,v){this[k]=v;}}; this.hidden = false; this.open = false; this.disabled = false;
    this.value = ''; this.scrollTop = 0; this.scrollHeight = 1200; this.clientHeight = 600;
  }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get id() { return this.attrs.id || ''; } set id(v) { this.attrs.id = v; }
  get textContent() { return this.nodeType === 3 ? this._text : this.childNodes.map(x => x.textContent).join(''); }
  set textContent(value) { if (this.nodeType === 3) this._text = String(value); else this.replaceChildren(new Node('', String(value))); }
  set innerHTML(_) { throw new Error('Unsafe innerHTML assignment'); }
  set outerHTML(_) { throw new Error('Unsafe outerHTML assignment'); }
  append(...nodes) { for (let node of nodes) { if (!(node instanceof Node)) node = new Node('', String(node)); if(node.parentNode) node.remove(); node.parentNode = this; this.childNodes.push(node); } }
  appendChild(node) { this.append(node); return node; }
  prepend(...nodes) { const existing = this.childNodes.slice(); this.replaceChildren(...nodes, ...existing); }
  replaceChildren(...nodes) { this.childNodes.forEach(n => n.parentNode = null); this.childNodes = []; this.append(...nodes); }
  replaceWith(node) { if (!this.parentNode) return; const parent = this.parentNode; const index = parent.childNodes.indexOf(this); this.remove(); node.parentNode = parent; parent.childNodes.splice(index,0,node); }
  remove() { if (this.parentNode) { const array = this.parentNode.childNodes; const index = array.indexOf(this); if (index >= 0) array.splice(index,1); this.parentNode = null; } }
  setAttribute(name, value) { value=String(value); this.attrs[name]=value; if(name==='class')this.className=value; else if(name==='hidden')this.hidden=true; else if(name==='open')this.open=true; else if(name==='value')this.value=value; else if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value; else if(['src','href','title','type'].includes(name))this[name]=value; }
  getAttribute(name) { if(name==='class')return this.className; if(name==='open')return this.open?'':null; if(name==='src'&&this.src)return this.src; return Object.prototype.hasOwnProperty.call(this.attrs,name)?this.attrs[name]:null; }
  removeAttribute(name) { delete this.attrs[name]; if(name==='open')this.open=false; if(name==='hidden')this.hidden=false; }
  hasAttribute(name){return this.getAttribute(name)!==null;}
  querySelectorAll(selector) { const selectors=selector.split(',').map(x=>x.trim()); const result=[]; const walk=node=>node.children.forEach(child=>{if(selectors.some(s=>matches(child,s)))result.push(child);walk(child);});walk(this);return result; }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
  closest(selector) { let node=this;while(node){if(matches(node,selector))return node;node=node.parentNode;}return null; }
  addEventListener(name, fn) { (this.events[name] ||= []).push(fn); }
  removeEventListener(name,fn){this.events[name]=(this.events[name]||[]).filter(v=>v!==fn);}
  dispatchEvent(event) { event.target ||= this;event.currentTarget=this;event.preventDefault ||= ()=>{}; for(const fn of this.events[event.type]||[]){try{const returned=fn.call(this,event);if(returned&&returned.then)track(returned);}catch(e){errors.push(e.stack);}}return true; }
  click(){if(!this.disabled)this.dispatchEvent({type:'click'});}
  focus(){document.activeElement=this;}select(){this.selected=true;}scrollIntoView(){this.scrolledIntoView=true;}
  getBoundingClientRect(){return {left:0,top:0,right:900,bottom:900,width:900,height:900};}
  showModal(){this.open=true;}close(){if(!this.open)return;this.open=false;this.dispatchEvent({type:'close'});}
  get isConnected(){let n=this;while(n){if(n===document)return true;n=n.parentNode;}return false;}
}
function build(tree) { if ('text' in tree)return new Node('',tree.text);const node=new Node(tree.tag);for(const [k,v]of Object.entries(tree.attrs||{}))node.setAttribute(k,v===null?'':v);for(const child of tree.children||[])node.append(build(child));return node; }
let document;
function track(promise){pending.add(promise);promise.catch(e=>errors.push(e.stack)).finally(()=>pending.delete(promise));return promise;}
async function settle(){let idle=0;for(let i=0;i<100;i++){await new Promise(r=>setTimeout(r,10));if(pending.size){idle=0;await Promise.allSettled([...pending]);}await new Promise(r=>setTimeout(r,10));if(!pending.size){idle++;if(idle>=3)return;}else idle=0;}throw new Error('Pending UI requests did not settle');}
async function bootstrap(name, url) {
  for(const id of uiTimers) clearTimeout(id);uiTimers.clear();
  document=build(JSON.parse(fs.readFileSync(path.join(environment.tempRoot,name+'-tree.json'),'utf8')));
  document.body=document.querySelector('body');document.documentElement=document.querySelector('html');document.getElementById=id=>document.querySelector('#'+id);
  document.createElement=tag=>new Node(tag);document.createElementNS=(_,tag)=>new Node(tag);document.createTextNode=text=>new Node('',text);document.createDocumentFragment=()=>new Node('fragment');
  const location=new URL(url);const localHistory={replaceState(_s,_t,path){const next=new URL(path,location.href);location.href=next.href;}};
  const context={document,location,history:localHistory,URL,URLSearchParams,Intl,Date,Map,Set,Array,Object,String,Number,Math,JSON,RegExp,Promise,Error,Blob,AbortController,console,
    requestAnimationFrame:fn=>later(fn,0),setTimeout:later,clearTimeout:cancelLater,
    navigator:{clipboard:{writeText:async()=>{}}},
    fetch:(path,options={})=>{const entry={path,options};requests.push(entry);const op=fetch(new URL(path,config.base),Object.assign({},options,{headers:Object.assign({},options.headers,{Cookie:config.cookie,Origin:config.base})})).then(async response=>{entry.responseStatus=response.status;const data=await response.json();const modified=modify?modify(path,data):data;return {ok:response.ok,status:response.status,json:async()=>modified};});return track(op);},
    matchMedia:()=>({matches:false,addEventListener(){}}),
    addEventListener:(event,fn)=>{(handlers[event]||=[]).push(fn);},
    isSecureContext:true,innerWidth:1300,innerHeight:900
  }; context.window=context;context.self=context;context.Element=Node;context.HTMLElement=Node;
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(environment.tempRoot,name+'-script.js'),'utf8'),context,{timeout:5000});await settle();return context;
}
const byId=id=>document.getElementById(id);
function action(container,label){const result=container.querySelectorAll('button').find(b=>b.textContent.includes(label));assert(result,'Button missing: '+label);return result;}
let passed=0;
function check(name, fn){fn();passed++;console.log('PASS '+name);}
async function runChecks() {
  const context=await bootstrap('world',config.base+'/#workbench');
  check('real demo boot and explicit synthetic banner',()=>{assert(!byId('source-banner').hidden);assert(byId('source-banner-copy').textContent.includes('合成'));assert.strictEqual(byId('stat-messages').textContent,String(config.messages));assert(byId('opportunity-list').querySelectorAll('.opportunity').length>0);assert(!requests.some(r=>r.options.method==='POST'));});
  const initialCard=byId('opportunity-list').querySelector('.opportunity');
  action(initialCard,'查看').click();await settle();
  check('candidate opens complete evidence context',()=>{assert(byId('evidence-dialog').open);assert(byId('evidence-body').querySelector('.context-message.anchor'));assert(byId('evidence-body').querySelectorAll('.context-message').length>=2);});
  byId('evidence-history').click();await settle();
  check('evidence links to precise history message',()=>{assert(!byId('view-history').hidden);const url=byId('history-frame').src;assert(url.startsWith('/history?'));assert(url.includes('conversation=')&&url.includes('message='));});
  document.querySelector('[data-view="workbench"]').click();await settle();
  action(byId('opportunity-list').querySelector('.opportunity'),'记录判断').click();byId('review-status').value='follow_up';byId('review-note').value='UI_SYNTHETIC_REVIEW';byId('review-form').dispatchEvent({type:'submit'});await settle();
  document.querySelector('[data-status="follow_up"]').click();await settle();
  check('explicit review POST and local status update',()=>{assert(!byId('review-dialog').open);assert(byId('opportunity-list').textContent.includes('UI_SYNTHETIC_REVIEW'));assert(requests.some(r=>r.path==='/api/world/review'&&r.options.method==='POST'&&JSON.parse(r.options.body).status==='follow_up'&&typeof JSON.parse(r.options.body).source_fingerprint==='string'));});
  action(byId('opportunity-list').querySelector('.opportunity'),'拟跟进草稿').click();await settle();
  check('draft POST creates editable unsent rule template',()=>{assert(byId('draft-dialog').open);assert(byId('draft-body').value.includes('固定模板'));assert(requests.some(r=>r.path==='/api/world/draft'&&r.options.method==='POST'&&typeof JSON.parse(r.options.body).source_fingerprint==='string'));assert(byId('draft-list').textContent.includes('未发送'));});
  byId('draft-dialog').close();modify=(path,data)=>{if(path==='/api/world/drafts'&&data.items?.length){data.items[0].source_stale=true;data.items[0].source_note='SYNTHETIC_CHANGED_SOURCE';}return data;};byId('refresh-drafts').click();await settle();
  check('stale draft source warning shown',()=>{assert(byId('draft-list').textContent.includes('依据已变化'));});modify=null;
  document.querySelector('[data-view="timeline"]').click();await settle();byId('timeline-all').click();await settle();
  check('timeline all-records reset and evidence affordance',()=>{assert.strictEqual(byId('world-query').value,'');assert.strictEqual(byId('timeline-list').querySelectorAll('.timeline-entry').length,config.messages);assert(byId('timeline-list').textContent.includes('查看上下文'));});
  document.querySelector('[data-view="graph"]').click();await settle();
  check('deterministic evidence graph and edge inspector',()=>{assert(!byId('graph-layout').hidden);assert(byId('graph-svg').querySelectorAll('.graph-node').length>0);assert(byId('graph-svg').querySelectorAll('.graph-edge').length>0);assert(byId('graph-info').querySelectorAll('.edge-item').length>0);});
  byId('graph-info').querySelector('.edge-item').click();await settle();
  check('graph edge opens original evidence',()=>assert(byId('evidence-dialog').open));
  byId('evidence-dialog').close();modify=(path,data)=>{if(path.startsWith('/api/world/evidence')){data.message.text='<img src="https://invalid.example/x" onerror="alert(1)">';data.message.type='image';data.message.media_url='https://invalid.example/private';data.context=[];}return data;};byId('graph-info').querySelector('.edge-item').click();await settle();
  check('untrusted content is literal and external media is rejected',()=>{assert(byId('evidence-body').textContent.includes('<img src='));assert.strictEqual(byId('evidence-body').querySelectorAll('img').length,0);assert(!requests.some(r=>String(r.path).startsWith('http')));});modify=null;byId('evidence-dialog').close();
  document.querySelector('[data-view="workbench"]').click();await settle();modify=(path,data)=>{if(path.startsWith('/api/world/opportunities'))for(const item of data.items||[]){item.review_stale=true;item.status='completed';}return data;};byId('search-form').dispatchEvent({type:'submit'});await settle();document.querySelector('[data-status="needs_review"]').click();
  check('changed review evidence returns to visible needs-review lane',()=>{assert(byId('opportunity-list').textContent.includes('有新证据，请复核'));assert(byId('opportunity-list').textContent.includes('原标记：已完成'));});modify=null;
  // Simulate a view carrying an earlier fingerprint; the actual service must
  // reject the write and the UI must reload candidates without saving success.
  modify=(route,data)=>{if(route.startsWith('/api/world/opportunities'))for(const item of data.items||[])item.source_fingerprint='0'.repeat(64);return data;};
  byId('search-form').dispatchEvent({type:'submit'});await settle();modify=null;
  const readsBeforeConflict=requests.filter(r=>r.path.startsWith('/api/world/opportunities')).length;
  action(byId('opportunity-list').querySelector('.opportunity'),'记录判断').click();byId('review-status').value='completed';byId('review-note').value='UI_CONFLICT_MUST_NOT_SAVE';byId('review-form').dispatchEvent({type:'submit'});await settle();
  check('stale fingerprint yields 409 and reloads candidates without saving',()=>{assert(byId('review-dialog').open);assert(!byId('review-error').hidden);assert(byId('review-error').textContent.includes('依据'));assert(requests.some(r=>r.path==='/api/world/review'&&r.responseStatus===409));assert(requests.filter(r=>r.path.startsWith('/api/world/opportunities')).length>readsBeforeConflict);assert(!byId('opportunity-list').textContent.includes('UI_CONFLICT_MUST_NOT_SAVE'));});byId('review-dialog').close();
  byId('open-coverage').click();await settle();check('coverage reads real type counts',()=>{assert(byId('coverage-dialog').open);assert(byId('coverage-body').querySelector('table'));assert(byId('coverage-body').textContent.includes(String(config.messages)));});byId('coverage-dialog').close();
  modify=(path,data)=>path==='/api/world/overview'?{ready:false,demo:false,counts:{messages:0,conversations:0,media:0},notes:['SYNTHETIC_SOURCE_NOT_READY']}:data;byId('refresh-all').click();await settle();
  check('no-data state does not imply imported zero records',()=>{assert.strictEqual(byId('stat-messages').textContent,'—');assert(!byId('global-state').hidden);assert(byId('global-state').textContent.includes('SYNTHETIC_SOURCE_NOT_READY'));assert(byId('view-workbench').hidden);});modify=null;
  const before=requests.length;await bootstrap('world','file:///tmp/world.html');check('direct HTML gives offline import guidance without network requests',()=>{assert.strictEqual(requests.length,before);assert(byId('global-state').textContent.includes('本地启动程序'));});
  await bootstrap('index',config.base+'/history?conversation='+encodeURIComponent(config.cid)+'&message='+encodeURIComponent(config.mid));
  check('history conversation+message deep link loads and highlights exact record',()=>{assert(!byId('room-header').hidden);assert(byId('messages').querySelector('.message.focused'));assert(requests.some(r=>r.path.startsWith('/api/messages?')&&r.path.includes('around='+encodeURIComponent(config.mid))));});
  await bootstrap('index',config.base+'/history?message='+encodeURIComponent(config.mid));
  check('message-only history link resolves conversation through evidence API',()=>{assert(!byId('room-header').hidden);assert(byId('messages').querySelector('.message.focused'));});
  assert.strictEqual(errors.length,0,errors.join('\n'));console.log('UI_CHECK_COMPLETE '+passed+' DOM interaction/API integration groups. Synthetic data only; this is not browser visual verification.');
}
try { await runChecks(); } catch(error) { console.error(error.stack);process.exitCode=1; } finally { for(const id of uiTimers) clearTimeout(id);await environment.stop(); }
