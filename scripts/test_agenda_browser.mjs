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
const pythonCode="import sys,json\nfrom pathlib import Path\nfrom server import import_archive,ViewerServer\nfrom test_agenda import fixture_rows,NOW\nstate=Path(sys.argv[1]);state.mkdir(mode=0o700)\nrows=fixture_rows()\nchats=[dict(id=i,name='\u4eba\u5de5\u65e5\u7a0b\u4f1a\u8bdd',is_group=i=='group') for i in sorted({r['conversation_id'] for r in rows})]\nsource=state/'synthetic.json'\nsource.write_text(json.dumps(dict(manifest=dict(synthetic=True,complete=True),messages=rows,conversations=chats)))\nimport_archive(source,state)\nserver=ViewerServer(0,state,'synthetic-browser-session')\nserver.agenda.clock=lambda:NOW\nprint(json.dumps({'port':server.server_address[1]}),flush=True)\nserver.serve_forever()\n";
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
  await until(()=>evaluate("!!document.getElementById('agenda-entry')"),'agenda entry');
  await command('Page.navigate',{url:base+'/agenda'});
  await until(()=>evaluate("!!document.querySelector('#candidates .card') && !document.getElementById('refresh').disabled"),'agenda inbox');
  assert(await evaluate("!document.getElementById('demo').hidden && !document.querySelector('#events .card')"));
  assert(await evaluate("document.getElementById('calendar-scope').textContent.includes('未导入')"));
  await screenshot('agenda-desktop');console.log('PASS synthetic agenda inbox and limited calendar coverage');
  await evaluate("[...document.querySelectorAll('#candidates .card')].find(c=>c.textContent.includes('地点：测试会议室')).querySelectorAll('button')[1].click()");
  await until(()=>evaluate("document.getElementById('editor').open"),'event editor');
  assert.strictEqual(await evaluate("document.getElementById('start').value"),'2030-06-11T15:00');
  assert.strictEqual(await evaluate("document.getElementById('end').value"),'2030-06-11T16:00');
  await evaluate("document.getElementById('title').value='人工安排 · 机械臂演示';document.getElementById('before').value='20';document.getElementById('after').value='10';document.getElementById('reviewed').checked=true;document.getElementById('ack-scope').checked=true;document.getElementById('preview').click()");
  await until(()=>evaluate("!document.getElementById('save').disabled"),'conflict check');
  await screenshot('agenda-confirm-preview');
  await evaluate("document.getElementById('save').click()");
  await until(()=>evaluate("!document.getElementById('editor').open && document.querySelector('#events .card') && !document.getElementById('refresh').disabled"),'confirmed itinerary');
  assert(await evaluate("document.getElementById('events').textContent.includes('20 分钟')"));
  const prior=await evaluate('performance.timeOrigin');await command('Page.reload');
  await until(()=>evaluate('performance.timeOrigin!=='+prior+" && document.getElementById('events')?.textContent.includes('机械臂演示') && !document.getElementById('refresh').disabled").catch(e=>{if(/context|navigat/i.test(e.message))return false;throw e;}),'persisted itinerary');
  console.log('PASS source dates, explicit confirmation, travel buffer and persistence');
  await evaluate("document.getElementById('manual').click();document.getElementById('title').value='冲突演示';document.getElementById('start').value='2030-06-11T16:15';document.getElementById('end').value='2030-06-11T17:00';document.getElementById('before').value='20';document.getElementById('reviewed').checked=true;document.getElementById('ack-scope').checked=true;document.getElementById('preview').click()");
  await until(()=>evaluate("document.querySelector('#preview-result .conflict') && !document.getElementById('preview').disabled"),'buffer conflict');
  assert(await evaluate("document.getElementById('save').disabled && document.getElementById('preview-result').textContent.includes('不能擅自改变')"));
  await screenshot('agenda-conflict');
  // No save: close the unchanged synthetic manual draft (programmatic test fields did not trigger dirty input).
  await evaluate("document.querySelector('#editor [data-close]').click()");
  console.log('PASS conflicting preparation/travel windows block save; alternatives remain proposals');
  await evaluate("document.getElementById('select-valid').click();document.getElementById('export').click()");
  await until(()=>evaluate("document.getElementById('export-dialog').open"),'export dialog');
  assert(await evaluate("document.getElementById('download').disabled"));
  await evaluate("document.getElementById('export-ack').checked=true;document.getElementById('with-buffers').checked=true;document.getElementById('build-export').click()");
  await until(()=>evaluate("!document.getElementById('download').disabled"),'calendar export');
  const ics=await evaluate("document.getElementById('export-text').value");assert.strictEqual((ics.match(/BEGIN:VEVENT/g)||[]).length,3);assert(!ics.includes('demo:agenda:')&&!ics.includes('ATTENDEE')&&!ics.includes('VALARM'));
  await screenshot('agenda-export');await evaluate("document.getElementById('export-dialog').close()");
  console.log('PASS selected-only ICS export without attendees, alarms or chat evidence');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await delay(200);
  assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));await screenshot('agenda-mobile');
  await evaluate("document.querySelector('#events .actions button').click()");
  await until(()=>evaluate("document.getElementById('evidence-body').textContent.includes('明天下午三点')"),'evidence linkage');
  await evaluate("document.getElementById('evidence').close()");
  const pendingDialog=new Promise(resolve=>{const handler=event=>{const item=JSON.parse(event.data);if(item.method==='Page.javascriptDialogOpening'){ws.removeEventListener('message',handler);resolve(item.params);}};ws.addEventListener('message',handler);});
  const click=evaluate("[...document.querySelectorAll('#events .actions button')].find(b=>b.textContent==='取消本机安排').click()");
  const dialog=await Promise.race([pendingDialog,delay(10000).then(()=>{throw new Error('Missing local cancellation confirmation');})]);assert(dialog.message.includes('不通知'));
  await command('Page.handleJavaScriptDialog',{accept:true});await click;
  await until(()=>evaluate("!document.querySelector('#events .card') && document.querySelector('#past-events .card') && !document.getElementById('refresh').disabled"),'local cancel');
  console.log('PASS evidence, mobile layout and explicitly local-only cancellation');
  assert.deepEqual(pageErrors,[]);
  // Inline data: images are document-local bytes, not external fetches.
  const unexpected=network.filter(url=>!url.startsWith(base+'/')&&url!=='about:blank'&&!url.startsWith('data:image/'));
  if(unexpected.length)console.log('UNEXPECTED_REQUEST_ORIGINS '+JSON.stringify(unexpected.map(url=>new URL(url).origin)));
  assert.strictEqual(unexpected.length,0,'The synthetic application must not fetch external resources');
  console.log('AGENDA_BROWSER_CHECK_COMPLETE 5 groups; synthetic data only'+(output?'; screenshots: '+output:''));
  await command('Browser.close').catch(()=>{});
}catch(error){console.error(error.stack);process.exitCode=1;}
finally{if(ws)ws.close();for(const p of pending.values())p.reject(new Error('Test finished'));pending.clear();await stop(browser);await stop(python);fs.rmSync(tmp,{recursive:true,force:true,maxRetries:10,retryDelay:100});}
