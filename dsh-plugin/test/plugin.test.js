import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {apply,localBase,localRequest,serviceStatus,portal,views} from '../index.js';

const request=(headers={},remote='127.0.0.1')=>({socket:{remoteAddress:remote},headers:{host:'127.0.0.1:3000',...headers}});

test('workspace URL cannot point to a remote host or arbitrary route',()=>{
 assert.equal(localBase(), 'http://127.0.0.1:18792');
 for(const url of ['https://example.com','http://localhost:12/','http://127.0.0.1:12/api/private','http://u:p@127.0.0.1/','http://127.0.0.1/?q=x','http://127.0.0.1/#x'])assert.throws(()=>localBase(url));
});
test('host route rejects remote and forwarded requests',()=>{
 assert(localRequest(request()));
 assert(localRequest(request({origin:'http://127.0.0.1:3000'})));
 assert(!localRequest(request({},'192.168.1.5')));
 for(const headers of [{origin:'https://other.invalid'},{host:'evil.invalid'},{'x-forwarded-for':'127.0.0.1'},{'sec-fetch-site':'same-site'},{'sec-fetch-site':'cross-site'}])assert(!localRequest(request(headers)));
});
test('status only probes the allowlisted health resource',async()=>{
 const old=globalThis.fetch;const urls=[];
 globalThis.fetch=async(url,options)=>{urls.push(url);assert.equal(options.redirect,'error');return new Response(JSON.stringify({app:'aiman-personal-world',version:'0.4.0',private:'never returned'}));};
 try{assert.deepEqual(await serviceStatus('http://127.0.0.1:18792'),{ready:true,version:'0.4.0'});assert.deepEqual(urls,['http://127.0.0.1:18792/healthz']);}finally{globalThis.fetch=old;}
});
test('wrong or unavailable service does not look connected',async()=>{
 const old=globalThis.fetch;
 try{
  globalThis.fetch=async()=>new Response(JSON.stringify({app:'different-app',version:'1'}));assert.deepEqual(await serviceStatus('http://127.0.0.1:123'),{ready:false});
  globalThis.fetch=async()=>{throw new Error('offline');};assert.deepEqual(await serviceStatus('http://127.0.0.1:123'),{ready:false});
 }finally{globalThis.fetch=old;}
});
test('all eight original modules have local entries without private material',()=>{
 const html=portal(localBase());
 for(const route of Object.values(views))assert(html.includes('http://127.0.0.1:18792'+route));
 assert(!html.includes('access='));assert(!html.includes('<iframe'));
 assert(html.includes('不是自动读取整个微信库'));
});
test('native tools register and navigation never reads or modifies data',async()=>{
 const tools=new Map();let route,cleanup,disposed=false,prompt;const effects=[];
 const ctx={tools:{register:t=>tools.set(t.name,t)},systemPrompt:{section:s=>{prompt=s.text;}},webServer:{register:r=>{route=r;return()=>{disposed=true;};}},effect:f=>{cleanup=f();effects.push(cleanup);}};
 apply(ctx,{workspaceUrl:'http://127.0.0.1:12345/'});
 assert.equal(tools.size,6);assert(prompt.includes('Do not claim to have read'));
 const tool=tools.get('personal_world_navigation');const value=JSON.parse((await tool.execute({view:'agenda'})).text);
 assert.equal(value.url,'http://127.0.0.1:12345/agenda');assert.equal(value.privateContentRead,false);
 await assert.rejects(()=>tool.execute({view:'../../etc'}));
 const output={writeHead(code,headers){this.code=code;this.headers=headers;},end(body){this.body=body;}};
 route.handler({...request(),method:'POST',url:'/personal-world/'},output);assert.equal(output.code,405);
 route.handler({...request(),method:'GET',url:'/personal-world/'},output);assert.equal(output.code,200);assert(output.body.includes('个人世界'));
 for(const effect of effects)effect();assert(disposed);
});
test('browser plugin uses the real DSH sidebar slot and cleans it up',()=>{
 let descriptor,callback,slot,unregistered=false,removed=false,cleanup,opened;
 const context={window:{__ModuleLoader__:{load:d=>{descriptor=d;}},open:(...args)=>{opened=args;}}};
 vm.runInNewContext(fs.readFileSync(new URL('../client.js',import.meta.url),'utf8'),context);
 assert.equal(descriptor.id,'@aiman-world/dsh-personal-world');
 const React={createElement:(tag,props,...children)=>({tag,props,children})};const module=descriptor.factory(n=>{assert.equal(n,'react');return React;});
 const slots={inject(name,fn){assert.equal(name,'sidebar.footer.action');callback=fn;return()=>{removed=true;};},register(options,render){slot={options,render};return()=>{unregistered=true;};}};
 assert.deepEqual(Array.from(module.inject),['slots']);
 module.apply({get:()=>slots,effect:f=>{cleanup=f();}});callback();
 assert.equal(slot.options.id,'aiman-personal-world');const tree=slot.render({wide:true});const button=tree.tag(tree.props);button.props.onClick();
 assert.deepEqual(Array.from(opened),['/personal-world/','_blank','noopener,noreferrer']);cleanup();assert(unregistered&&removed);
});
test('npm package is a native plugin and includes the complete local core',()=>{
 const pkg=JSON.parse(fs.readFileSync(new URL('../../package.json',import.meta.url),'utf8'));
 assert.equal(pkg.dsh.engines.dsh,'0.2.0-rc.2');assert.deepEqual(pkg.dsh.client.inject,['@deepseek-ai/dsh-client-ui-renderer','@deepseek-ai/dsh-client-ui-sidebar']);assert.equal(pkg.dsh.bundle.patch,'./dsh-plugin/cordis.patch.yml');
 for(const file of ['server.py','personal_world.py','personal_agenda.py','personal_profile.py','world_model.py','world.html','index.html','profile.html','agenda.html','agenda.js','launch.py'])assert(fs.existsSync(new URL('../../'+file,import.meta.url)));
 assert(!pkg.scripts?.postinstall);assert(!pkg.dependencies);
});
