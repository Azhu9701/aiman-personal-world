// This local UI receives only the file selection made by the owner. No
// original archive is read and no model-facing tool can import a selection.
import {randomUUID,createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {LIMITS,SelectionError} from './selection-store.js';
const headers={ 'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"};
function respond(res,code,value,extra={}){const body=typeof value==='string'?value:JSON.stringify(value);res.writeHead(code,{...headers,'Content-Type':'application/json;charset=utf-8',...extra});res.end(body);}
function cookieName(req){return 'pw_selection_'+createHash('sha256').update(req.headers.host||'').digest('hex').slice(0,10);}
function readCookie(req){const name=cookieName(req);return (req.headers.cookie||'').split(';').map(s=>s.trim()).find(s=>s.startsWith(name+'='))?.slice(name.length+1);}
async function body(req){
 if(req.headers['content-type']!=='application/json')throw new SelectionError('invalid_input','JSON required');
 const size=Number(req.headers['content-length']);
 if(!Number.isSafeInteger(size)||size<1||size>LIMITS.importBytes+16384)throw new SelectionError('invalid_input','Body length is invalid or exceeds the selection limit');
 const chunks=[];let total=0;req.setTimeout?.(10000,()=>req.destroy());
 for await(const chunk of req){total+=chunk.length;if(total>size||total>LIMITS.importBytes+16384)throw new SelectionError('invalid_input','Body exceeded its limit');chunks.push(chunk);}
 if(total!==size)throw new SelectionError('invalid_input','Incomplete upload');return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
export function selectionHandler(store){
 const sessions=new Map();
 const page=readFileSync(new URL('./agent-selection.html',import.meta.url),'utf8');
 const script=readFileSync(new URL('./agent-selection.js',import.meta.url),'utf8');
 function owner(req){const key=readCookie(req),expires=key&&sessions.get(key);return expires&&expires>Date.now()?key:null;}
 return async(req,res)=>{
  try{
   const route=new URL(req.url,'http://localhost').pathname;
   if(req.method==='GET'&&(route==='/personal-world/agent'||route==='/personal-world/agent/')){
    for(const [key,expires] of sessions)if(expires<Date.now())sessions.delete(key);
    let key=owner(req);if(!key){if(sessions.size>=64)return respond(res,429,{error:'Too many local selection sessions'});key=randomUUID();sessions.set(key,Date.now()+LIMITS.selectionLifetimeMs);}
    return respond(res,200,page,{'Content-Type':'text/html;charset=utf-8','Set-Cookie':cookieName(req)+'='+key+'; HttpOnly; SameSite=Strict; Path=/personal-world/agent; Max-Age=3600'});
   }
   if(req.method==='GET'&&route==='/personal-world/agent/app.js')return respond(res,200,script,{'Content-Type':'text/javascript;charset=utf-8'});
   const key=owner(req);if(!key)return respond(res,401,{ok:false,error:'Open the owner selection page first'});
   if(req.method==='GET'&&route==='/personal-world/agent/selections')return respond(res,200,{ok:true,selections:store.list(key)});
   if(req.method!=='POST')return respond(res,405,{ok:false,error:'Unsupported operation'});
   if(req.headers.origin!=='http://'+req.headers.host)return respond(res,403,{ok:false,error:'Same-origin owner action required'});
   const data=await body(req);
   if(route==='/personal-world/agent/import'){
    if(!data||Object.keys(data).sort().join(',')!=='acknowledge_model_disclosure,selection'||data.acknowledge_model_disclosure!==true)return respond(res,400,{ok:false,error:'Review the selected scope and acknowledge model/log disclosure'});
    return respond(res,200,{ok:true,...store.import(key,data.selection),stored_on_disk:false,model_called:false});
   }
   if(route==='/personal-world/agent/revoke'){
    if(!data||Object.keys(data).join(',')!=='selection_id'||typeof data.selection_id!=='string')return respond(res,400,{ok:false,error:'Invalid selection'});
    return respond(res,200,{ok:store.revoke(key,data.selection_id),already_returned_content_erased:false});
   }
   return respond(res,404,{ok:false,error:'No such owner action'});
  }catch(error){return respond(res,error instanceof SelectionError?400:500,{ok:false,error:error instanceof SelectionError?error.message:'The selected operation could not be completed'});}
 };
}
