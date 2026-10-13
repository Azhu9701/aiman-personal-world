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
state=Path(sys.argv[1]);state.mkdir(mode=0o700)
import_archive(Path('examples/demo.json'),state)
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
  await until(()=>evaluate("!!document.querySelector('.opportunity') && !document.getElementById('source-banner').hidden"),'rendered synthetic workbench');
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));await screenshot('workbench-desktop');
  console.log('PASS actual Chromium synthetic workbench and desktop layout');
  await evaluate("Array.from(document.querySelector('.opportunity').querySelectorAll('button')).find(b=>b.textContent.includes('拟跟进草稿')).click()");
  await until(()=>evaluate("document.getElementById('draft-dialog').open && !!document.getElementById('draft-history-list').textContent"),'draft dialog');
  await evaluate("document.getElementById('draft-title-input').value='人工浏览器测试草稿';document.getElementById('draft-body').value='这是完全人工的测试文字。<script>literal only</script>';for(const id of ['draft-title-input','draft-body'])document.getElementById(id).dispatchEvent(new Event('input',{bubbles:true}));document.getElementById('draft-save').click()");
  await until(()=>evaluate("document.getElementById('draft-save-status').textContent.includes('第 2 版') && !document.getElementById('draft-body').disabled"),'saved revision');
  assert(await evaluate("document.getElementById('draft-history-list').querySelector('script')===null"));
  console.log('PASS real browser draft save and inert literal content');
  const oldTimeOrigin=await evaluate('performance.timeOrigin');
  await command('Page.reload');
  await until(()=>evaluate("performance.timeOrigin!=="+oldTimeOrigin+" && !!document.querySelector('#draft-list button') && document.getElementById('draft-list')?.textContent.includes('人工浏览器测试草稿')").catch(e=>{if(/context|navigat/i.test(e.message))return false;throw e;}),'draft after full page reload');
  await evaluate("document.querySelector('#draft-list button').click()");
  await until(()=>evaluate("document.getElementById('draft-body').value.includes('完全人工的测试文字') && document.getElementById('draft-history-list').textContent.includes('原始生成版')"),'persistent edit and original history');
  await screenshot('draft-editor-desktop');console.log('PASS full browser reload retains edited draft and version history');
  await evaluate("document.getElementById('draft-dialog').close()");
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await delay(250);
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));await screenshot('workbench-mobile');
  console.log('PASS mobile-width layout does not overflow');
  assert.deepEqual(pageErrors,[]);
  // Inline data: images are document-local bytes, not external fetches.
  const unexpected=network.filter(url=>!url.startsWith(base+'/')&&url!=='about:blank'&&!url.startsWith('data:image/'));
  if(unexpected.length)console.log('UNEXPECTED_REQUEST_ORIGINS '+JSON.stringify(unexpected.map(url=>new URL(url).origin)));
  assert.strictEqual(unexpected.length,0,'The synthetic application must not fetch external resources');
  console.log('BROWSER_CHECK_COMPLETE 4 groups; synthetic data only'+(output?'; screenshots: '+output:''));
  await command('Browser.close').catch(()=>{});
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{if(ws)ws.close();for(const p of pending.values())p.reject(new Error('Test finished'));pending.clear();await stop(browser);await stop(python);fs.rmSync(tmp,{recursive:true,force:true});}
