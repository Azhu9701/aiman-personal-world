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
async function evaluate(expression){const r=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});if(r.exceptionDetails)throw new Error('Synthetic page script failed: '+(r.exceptionDetails.exception?.description||r.exceptionDetails.text));return r.result.value;}
function command(method,params={}){return new Promise((resolve,reject)=>{const id=++commandId;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},15000);pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});ws.send(JSON.stringify({id,method,params}));});}
async function screenshot(name){if(!output)return;const r=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(r.data,'base64'),{mode:0o600});}
async function stop(child){if(!child||child.exitCode!==null)return;child.kill('SIGTERM');await Promise.race([new Promise(resolve=>child.once('exit',resolve)),delay(3000)]);if(child.exitCode===null)child.kill('SIGKILL');}
try{
  const base=process.env.DSH_VERIFICATION_URL;
  const targetBase=new URL(base);
  if(targetBase.protocol!=='http:'||targetBase.hostname!=='127.0.0.1'||targetBase.port!=='18805')throw new Error('Only the isolated DSH verification instance is allowed');
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
  const bootstrap=new URL(process.env.DSH_VERIFICATION_BOOTSTRAP||base+'/');
  if(bootstrap.origin!==base||bootstrap.pathname!=='/')throw new Error('Unexpected isolated bootstrap destination');
  // Use the normal one-time URL printed by this credential-free test DSH instance.
  // Never log it or disable the host's authentication fence.
  await command('Page.navigate',{url:bootstrap.href});
  await until(()=>evaluate("!!document.querySelector('[data-personal-world-entry]')"),'native DSH sidebar entry',35);
  // Dismiss the standard preview notice in this empty verification profile only.
  await evaluate("(()=>{if(document.body.innerText.includes('预览版说明')){const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='继续');b?.click();}})()");
  await delay(200);
  const visible=await evaluate("(()=>{const r=document.querySelector('[data-personal-world-entry]').getBoundingClientRect();return r.width>0&&r.height>0;})()");
  assert(visible);await screenshot('dsh-native-sidebar');
  console.log('PASS actual DSH client loaded the native Personal World sidebar button');
  await evaluate("document.querySelector('[data-personal-world-entry]').click()");
  const opened=await until(async()=>{const pages=await (await fetch('http://127.0.0.1:'+port+'/json/list')).json();return pages.some(p=>p.url===base+'/personal-world/');},'portal tab');assert(opened);
  await command('Page.navigate',{url:base+'/personal-world/'});
  await until(()=>evaluate("document.querySelectorAll('.grid a').length===8"),'native portal');
  assert(await evaluate("document.body.textContent.includes('不读取')||document.body.textContent.includes('不会自动加入模型上下文')"));
  await screenshot('dsh-personal-world-portal');
  console.log('PASS native sidebar opens the eight-module local workspace portal');
  assert.deepEqual(pageErrors,[]);
  // Inline data: images are document-local bytes, not external fetches.
  const external=network.filter(url=>/^https?:/.test(url)&&!url.startsWith(base+'/'));
  assert.strictEqual(external.length,0,'The isolated browser did not need an external website');
  console.log('DSH_NATIVE_BROWSER_CHECK_COMPLETE 2 groups; synthetic data only'+(output?'; screenshots: '+output:''));
  await command('Browser.close').catch(()=>{});
}catch(error){
  if(ws){
    await screenshot('dsh-native-failure').catch(()=>{});
    const diagnostics=await evaluate("({title:document.title,body:document.body?.innerText.slice(0,1800),moduleLoader:!!window.__ModuleLoader__,scripts:[...document.scripts].map(s=>s.src).filter(Boolean)})").catch(()=>({}));
    console.error('ISOLATED_UI_DIAGNOSTIC '+JSON.stringify({pageErrors,diagnostics}));
  }
  console.error(error.stack);process.exitCode=1;
}
finally{if(ws)ws.close();for(const p of pending.values())p.reject(new Error('Test finished'));pending.clear();await stop(browser);await stop(python);fs.rmSync(tmp,{recursive:true,force:true,maxRetries:10,retryDelay:100});}
