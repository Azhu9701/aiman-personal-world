/** Real Chromium smoke test. Only the handwritten synthetic fixture is loaded.
 * Uses a new, temporary browser profile, never your existing Chrome session.
 * PYTHON=python3 CHROME_BIN=/path/to/chrome node scripts/test_browser.mjs
 * Optional PW_VISUAL_OUTPUT stores synthetic screenshots outside the repository.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const chrome=process.env.CHROME_BIN || (process.platform==='darwin'?'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome':null);
if(!chrome || !fs.existsSync(chrome))throw new Error('Set CHROME_BIN to an installed Chromium binary. No browser is downloaded.');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'personal-world-synthetic-browser-'));fs.chmodSync(tmp,0o700);
const output=process.env.PW_VISUAL_OUTPUT?path.resolve(process.env.PW_VISUAL_OUTPUT):null;
if(output && (output===root || output.startsWith(root+path.sep)))throw new Error('Keep screenshots outside the public source tree.');
if(output)fs.mkdirSync(output,{recursive:true,mode:0o700});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(test,label,seconds=20){const end=Date.now()+seconds*1000;while(Date.now()<end){const value=await test();if(value)return value;await delay(80);}throw new Error('Timed out: '+label);}
const pythonCode=`import sys,json
from pathlib import Path
from server import import_archive,ViewerServer
from personal_world import canonical_archive
state=Path(sys.argv[1]);state.mkdir(mode=0o700)
canonical_archive(Path('examples/profile-demo.json'),state/'archive.jsonl')
import_archive(state/'archive.jsonl',state)
server=ViewerServer(0,state,'synthetic-browser-session')
print(json.dumps({'port':server.server_address[1]}),flush=True)
server.serve_forever()
`;
let python, browser, ws;let commandId=0;const pending=new Map();const pageErrors=[];const network=[];
async function evaluate(expression){const r=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw new Error('Synthetic page script failed: '+(r.exceptionDetails.exception?.description||r.exceptionDetails.text));return r.result.value;}
function command(method,params={}){return new Promise((resolve,reject)=>{const id=++commandId;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},15000);pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});ws.send(JSON.stringify({id,method,params}));});}
async function screenshot(name){if(!output)return;const r=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(r.data,'base64'),{mode:0o600});}
async function stop(child){if(!child||child.exitCode!==null)return;child.kill('SIGTERM');await Promise.race([new Promise(resolve=>child.once('exit',resolve)),delay(3000)]);if(child.exitCode===null)child.kill('SIGKILL');}
try{
  python=spawn(process.env.PYTHON||'python3',['-B','-u','-c',pythonCode,path.join(tmp,'state')],{cwd:root,stdio:['ignore','pipe','pipe']});
  let startup='';python.stdout.on('data',data=>startup+=data);python.stderr.on('data',()=>{});
  python.on('error',e=>pageErrors.push('Python: '+e.message));
  const config=await until(()=>{if(python.exitCode!==null)throw new Error('Synthetic service did not start');try{return JSON.parse(startup.trim());}catch{return null;}},'synthetic service');
  const base='http://127.0.0.1:'+config.port;
  const profile=path.join(tmp,'browser');
  browser=spawn(chrome,['--headless=new','--remote-debugging-port=0','--user-data-dir='+profile,'--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-sync','--disable-extensions','--disable-component-update','--disable-features=MediaRouter','about:blank'],{stdio:'ignore'});
  browser.on('error',e=>pageErrors.push('Chromium: '+e.message));
  const portFile=path.join(profile,'DevToolsActivePort');await until(()=>fs.existsSync(portFile),'temporary browser');
  const port=fs.readFileSync(portFile,'utf8').split('\n')[0];assert.match(port,/^\d+$/);
  const target=await (await fetch('http://127.0.0.1:'+port+'/json/new?about:blank',{method:'PUT'})).json();
  ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
  ws.addEventListener('message',event=>{const data=JSON.parse(event.data);if(data.id){const p=pending.get(data.id);if(p){pending.delete(data.id);data.error?p.reject(new Error(data.error.message)):p.resolve(data.result);}}else if(data.method==='Runtime.exceptionThrown')pageErrors.push(data.params.exceptionDetails.text);else if(data.method==='Network.requestWillBeSent')network.push(data.params.request.url);});
  ws.addEventListener('close',()=>{for(const p of pending.values())p.reject(new Error('Temporary browser closed'));pending.clear();});
  await command('Page.enable');await command('Runtime.enable');await command('Network.enable');
  await command('Emulation.setDeviceMetricsOverride',{width:1280,height:960,deviceScaleFactor:1,mobile:false});
  await command('Page.navigate',{url:base+'/?access=synthetic-browser-session'});
  await until(()=>evaluate("!!document.getElementById('profile-entry')"),'main profile entry');
  await command('Page.navigate',{url:base+'/profile'});
  await until(()=>evaluate("!!document.querySelector('#candidates .card') && document.getElementById('candidates').textContent.includes('我坚持研究过程保留原始证据。') && !document.getElementById('load').disabled"),'self profile');
  assert(await evaluate("!document.getElementById('demo').hidden && !document.getElementById('candidates').textContent.includes('别的账号')"));
  await screenshot('personal-profile-desktop');console.log('PASS self profile renders literal candidates with synthetic label');
  await evaluate("[...document.querySelectorAll('#candidates .card')].find(c=>c.textContent.includes('我坚持研究过程保留原始证据。')).querySelectorAll('button')[1].click()");
  await until(()=>evaluate("document.getElementById('edit-dialog').open"),'profile editor');
  await evaluate("document.getElementById('edit-text').value='我坚持先核对证据，再决定是否推进。';document.getElementById('confirm').click()");
  await until(()=>evaluate("!document.getElementById('edit-dialog').open && document.getElementById('confirmed').textContent.includes('我坚持先核对证据，再决定是否推进。') && !document.getElementById('load').disabled"),'confirmed profile');
  const previousOrigin=await evaluate('performance.timeOrigin');await command('Page.reload');
  await until(()=>evaluate('performance.timeOrigin!=='+previousOrigin+" && document.getElementById('confirmed')?.textContent.includes('我坚持先核对证据，再决定是否推进。') && !document.getElementById('load').disabled").catch(e=>{if(/context|navigat/i.test(e.message))return false;throw e;}),'persistent profile');
  await evaluate("document.querySelector('#confirmed .actions button').click()");
  await until(()=>evaluate("document.getElementById('evidence-dialog').open && document.getElementById('evidence-body').textContent.includes('我坚持研究过程保留原始证据。')"),'original evidence');
  assert(await evaluate("document.getElementById('history-link').href.includes('message=')"));await evaluate("document.getElementById('evidence-dialog').close()");
  console.log('PASS edited profile persists and original evidence stays literal');
  await evaluate("document.getElementById('select-valid').click();document.getElementById('export').click()");
  await until(()=>evaluate("document.getElementById('export-dialog').open && document.getElementById('export-text').value.includes('我坚持先核对证据，再决定是否推进。')"),'selected export preview');
  const exported=await evaluate("document.getElementById('export-text').value");assert(!exported.includes('self-one')&&!exported.includes('人工画像示例会话')&&!exported.includes('我更喜欢先做本地验证'));
  await screenshot('personal-profile-export');await evaluate("document.getElementById('export-dialog').close()");console.log('PASS export preview contains only chosen confirmed prose');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await delay(200);
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));await screenshot('personal-profile-mobile');
  // Test deletion in this temporary synthetic profile. Accept only this test dialog.
  const pendingDialog=new Promise(resolve=>{const handler=event=>{const item=JSON.parse(event.data);if(item.method==='Page.javascriptDialogOpening'){ws.removeEventListener('message',handler);resolve(item.params);}};ws.addEventListener('message',handler);});
  const deleteClick=evaluate("[...document.querySelectorAll('#confirmed .actions button')].find(b=>b.textContent==='删除这条').click()");
  const dialog=await Promise.race([pendingDialog,delay(10000).then(()=>{throw new Error('No synthetic delete confirmation');})]);
  assert(dialog.message.includes('原聊天'));await command('Page.handleJavaScriptDialog',{accept:true});await deleteClick;
  await until(()=>evaluate("!document.querySelector('#confirmed .card') && !document.getElementById('load').disabled"),'profile deletion');
  assert(await evaluate("!document.getElementById('candidates').textContent.includes('我坚持研究过程保留原始证据。')"));
  const stored=JSON.parse(fs.readFileSync(path.join(tmp,'state','profile-state.json'),'utf8'));assert(!JSON.stringify(stored).includes('再决定是否推进'));assert.strictEqual(stored.suppressed.length,1);
  console.log('PASS explicit synthetic deletion removes prose and mobile layout fits');
  assert.deepEqual(pageErrors,[]);
  // Inline data: images are document-local bytes, not external fetches.
  const unexpected=network.filter(url=>!url.startsWith(base+'/')&&url!=='about:blank'&&!url.startsWith('data:image/'));
  if(unexpected.length)console.log('UNEXPECTED_REQUEST_ORIGINS '+JSON.stringify(unexpected.map(url=>new URL(url).origin)));
  assert.strictEqual(unexpected.length,0,'The synthetic application must not fetch external resources');
  console.log('PROFILE_BROWSER_CHECK_COMPLETE 4 groups; synthetic data only'+(output?'; screenshots: '+output:''));
  await command('Browser.close').catch(()=>{});
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{if(ws)ws.close();for(const p of pending.values())p.reject(new Error('Test finished'));pending.clear();await stop(browser);await stop(python);fs.rmSync(tmp,{recursive:true,force:true});}
