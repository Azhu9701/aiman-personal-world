// Native DSH host entry. This edition exposes local navigation, not private data.
export const name='aiman-personal-world';
export const inject=['webServer','tools','systemPrompt'];
export const views=Object.freeze({home:'/',history:'/history',timeline:'/#timeline',graph:'/#graph',profile:'/profile',agenda:'/agenda',actions:'/#workbench',sources:'/'});

export function localBase(value){
  const url=new URL(value||'http://127.0.0.1:18792/');
  if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||url.username||url.password||url.search||url.hash||url.pathname!=='/')throw new Error('A loopback workspace URL is required');
  return url.origin;
}
export function localRequest(req){
  if(!['127.0.0.1','::1','::ffff:127.0.0.1'].includes(req.socket?.remoteAddress))return false;
  if(req.headers.forwarded||req.headers['x-forwarded-for']||req.headers['x-forwarded-host'])return false;
  let url;try{url=new URL('http://'+req.headers.host);}catch{return false;}
  if(!['127.0.0.1','localhost','[::1]'].includes(url.hostname)||url.username||url.password||url.pathname!=='/'||url.search||url.hash)return false;
  if(req.headers.origin&&req.headers.origin!==url.origin)return false;
  return !['cross-site','same-site'].includes(req.headers['sec-fetch-site']);
}
export async function serviceStatus(base,signal){
  try{
    const response=await fetch(base+'/healthz',{redirect:'error',signal:signal?AbortSignal.any([signal,AbortSignal.timeout(2000)]):AbortSignal.timeout(2000)});
    if(!response.ok)return {ready:false};
    const reader=response.body.getReader();let size=0;const chunks=[];
    try{while(true){const next=await reader.read();if(next.done)break;size+=next.value.length;if(size>8192){await reader.cancel();return {ready:false};}chunks.push(next.value);}}finally{reader.releaseLock();}
    const value=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    return value.app==='aiman-personal-world'?{ready:true,version:String(value.version).slice(0,40)}:{ready:false};
  }catch{return {ready:false};}
}
export function description(base,status){
  return {plugin:'@aiman-world/dsh-personal-world',pluginVersion:'0.4.0',...status,
    modules:Object.keys(views),workspaceUrl:base,modelDataAccess:false,automaticCalendarWrite:false,
    note:'完整聊天、画像、日程与行动界面在本机打开。此版不把私人记录传给模型，不自动确认日程。'};
}
function json(res,code,data){const body=JSON.stringify(data);res.writeHead(code,{'Content-Type':'application/json;charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(body);}
function escape(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
export function portal(base){
 const labels={home:'个人世界总览',history:'微信历史与搜索',timeline:'人生时间线',graph:'关系与主题图谱',profile:'个人画像',agenda:'日程与行程',actions:'行动工作台与草稿',sources:'数据来源与历史导入'};
 return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>个人世界 · DeepSeek Harness</title><style>body{margin:0;background:#f7f9f3;color:#34493c;font:16px/1.7 system-ui,sans-serif}main{max-width:1000px;margin:auto;padding:42px 24px}h1{font-size:32px}p{color:#667466}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}a{display:block;background:white;border:1px solid #dfe5dc;border-radius:12px;padding:22px;color:#426942;text-decoration:none}a:hover{background:#eef3e8}small{display:block;color:#718071}.note{padding:16px;border:1px solid #e0d7ba;background:#fffbef;border-radius:10px;margin:22px 0}</style><main><small>AIMAN PERSONAL WORLD / DEEPSEEK HARNESS</small><h1>你的经历，仍由你掌握</h1><p>整个工作台已包含在插件包中。点击模块，在独立的本机页面中查看和操作；私人记录不会自动加入模型上下文。</p><div class="note">首次使用需启动插件包中的本机服务。此入口不会静默上传聊天、授权模型读取、写入外部日历或发送消息。</div><div class="grid">'+Object.entries(views).map(([id,route])=>'<a href="'+escape(base+route)+'" target="_blank" rel="noopener noreferrer">'+labels[id]+'<small>在本机工作台打开 →</small></a>').join('')+'</div><p>本机服务：'+escape(base)+'。若未连接，请从桌面 Personal World 入口启动。日程候选必须核对原话、时间及取消改期后才可确认。</p><p>此版提供完整本机界面与 Harness 原生入口，尚未提供模型直接检索私聊的工具。</p></main></html>';
}
export function apply(ctx,config={}){
 const base=localBase(config.workspaceUrl||process.env.PERSONAL_WORLD_URL);
 const result=value=>({text:JSON.stringify(value)});
 ctx.tools.register({name:'personal_world_status',description:'Check the local Personal World service and its available modules. Returns no private chat, profile, agenda or draft content.',
   parameters:{type:'object',properties:{},additionalProperties:false},output:{schema:{type:'object',properties:{text:{type:'string'}},required:['text']},render:(_a,v)=>[{type:'text',text:v.text}]},
   async execute(_args,exec){return result(description(base,await serviceStatus(base,exec?.signal)));},isConcurrencySafe:()=>true});
 ctx.tools.register({name:'personal_world_navigation',description:'Return a link to an explicitly selected local workspace page. This does not read private data or open/send anything automatically.',
   parameters:{type:'object',properties:{view:{type:'string',enum:Object.keys(views)}},required:['view'],additionalProperties:false},output:{schema:{type:'object',properties:{text:{type:'string'}},required:['text']},render:(_a,v)=>[{type:'text',text:v.text}]},
   async execute(args){if(!args||!Object.hasOwn(views,args.view))throw new Error('Invalid view');return result({view:args.view,url:base+views[args.view],privateContentRead:false});},isConcurrencySafe:()=>true});
 ctx.systemPrompt.section({name,order:118,text:'Personal World is available as a local workspace. personal_world_status and personal_world_navigation disclose no private records. Do not claim to have read chats or confirmed appointments from these tools. Use the owner-controlled workspace for history, profiles, agenda, drafts and imports. This plugin does not send messages, create external calendar events or authorize a model to read private records.'});
 ctx.effect(()=>{
   const dispose=ctx.webServer.register({kind:'prefix',path:'/personal-world',handler:(req,res)=>{
     if(!localRequest(req))return json(res,403,{error:'Local origin required'});
     const route=new URL(req.url,'http://localhost').pathname;
     if(req.method!=='GET')return json(res,405,{error:'Read-only portal'});
     if(route==='/personal-world/status')return serviceStatus(base).then(s=>json(res,200,description(base,s)));
     if(route!=='/personal-world'&&route!=='/personal-world/')return json(res,404,{error:'Not found'});
     const body=portal(base);res.writeHead(200,{'Content-Type':'text/html;charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"});res.end(body);
   }});
   return dispose;
 },'personal-world local portal');
}
