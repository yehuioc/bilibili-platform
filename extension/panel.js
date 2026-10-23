import {serviceUrl} from './protocol.mjs';
const $=id=>document.getElementById(id);
let state,binding=null,invalid=false,ticking=false,chain=Promise.resolve(),pollAbort=null,offlineDelay=1000;
const wake=()=>pollAbort?.abort();
const save=()=>chrome.storage.local.set({connection:state});
const saveBinding=()=>chrome.storage.session.set({binding});
function status(text,online=false){$('status').textContent=text;$('status').dataset.online=String(online);}
function serial(fn){chain=chain.then(fn).catch(e=>{$('notice').textContent=e.message;});return chain;}
function allowed(raw){try{const u=new URL(raw);return u.protocol==='https:'&&!u.username&&!u.password&&(!u.port||u.port==='443')&&((u.hostname==='www.bilibili.com'&&(u.pathname==='/'||/^\/video\/BV[A-Za-z0-9]{10}\/?$/.test(u.pathname)))||(u.hostname==='space.bilibili.com'&&/^\/\d+\/favlist\/?$/.test(u.pathname)));}catch{return false;}}
async function request(path,body,token=state.token,signal){
  let response;
  try{response=await fetch(serviceUrl(state.base)+'/bridge/'+path,{method:'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:JSON.stringify(body),signal:signal||AbortSignal.timeout(25000)});}
  catch(e){if(e.name==='AbortError')throw e;status('后台离线 · 等待自动重连');throw Error('请确认本地后台正在运行。');}
  const data=await response.json();
  if(!response.ok){if(response.status===403&&path!=='pair'){invalid=true;status('配对失效 · 请重新配对');}throw Error(data.error||'连接失败');}
  return data;
}
function renderBinding(){ $('binding').textContent=binding?`已绑定标签页 ${binding.tab_id}\n${binding.url}`:'尚未绑定';$('control').checked=!!binding?.control;}
async function stop(){if(binding){binding={...binding,control:false,session:crypto.randomUUID()};await saveBinding();}renderBinding();}
async function bound(session,control=true){
  if(!binding||binding.session!==session||(control&&!binding.control))throw Error('用户已停止或重新绑定标签页');
  const tab=await chrome.tabs.get(binding.tab_id);
  if(!allowed(tab.url)){await stop();throw Error('绑定标签页已关闭或离开允许的B站范围');}
  binding.url=tab.url;return tab;
}
async function site(action,args,session,control=true){
  await bound(session,control);
  await chrome.scripting.executeScript({target:{tabId:binding.tab_id},files:['site.js']});
  const result=await chrome.scripting.executeScript({target:{tabId:binding.tab_id},func:async(a,p)=>await globalThis.biliWorkbenchRun(a,p),args:[action,args]});
  await bound(session,control);
  if(!result[0]?.result)throw Error('页面没有返回实际执行结果');
  return result[0].result;
}
async function navigate(url,session){
  if(!allowed(url))throw Error('只允许B站首页、视频和收藏页');
  await bound(session);const tabId=binding.tab_id;
  await chrome.tabs.update(tabId,{url});
  const deadline=Date.now()+14000;
  while(Date.now()<deadline){const tab=await bound(session);if(tab.status==='complete'&&new URL(tab.url).pathname===new URL(url).pathname){return await site('snapshot',{},session);}await new Promise(r=>setTimeout(r,200));}
  throw Error('页面未在时限内加载，未计为成功');
}
async function execute(command){
  const {session,action,args}=command;
  if(command.tab_id!==binding?.tab_id||Date.now()/1000>command.expires)throw Error('操作绑定或时限无效');
  let result=action==='navigate'?await navigate(args.url,session):await site(action,args,session);
  if(action==='click'&&result.navigate_to)result=await navigate(result.navigate_to,session);
  const tab=await bound(session);
  if(!result.page||result.page.url!==tab.url)throw Error('返回结果与实际标签页不一致');
  const value={tab_id:tab.id,action,executed_at:Date.now()/1000,...result};
  $('debug-result').textContent=JSON.stringify(value,null,2);return value;
}
async function poll(wait=0){
  if(!state.token||invalid)return;
  if(binding){try{await bound(binding.session,false);}catch{binding=null;await saveBinding();renderBinding();}}
  let result;const controller=new AbortController();pollAbort=controller;
  try{result=await request('poll',{acks:[],binding,wait},state.token,AbortSignal.any([controller.signal,AbortSignal.timeout(25000)]));offlineDelay=1000;}
  catch(e){if(e.name==='AbortError')return;offlineDelay=Math.min(10000,offlineDelay*2);throw e;}
  finally{if(pollAbort===controller)pollAbort=null;}
  if(result.stopped){await stop();$('notice').textContent='工作台已停止浏览器操作。';}
  status(binding?.control?'已连接 · 工作台可以操作绑定页面':'已连接 · 浏览器由你操作',true);
  if(result.messages?.length)await request('poll',{acks:result.messages.map(m=>m.id),binding});
  if(result.command){let value=null,error=null;try{value=await execute(result.command);}catch(e){error=e.message;$('debug-result').textContent=error;}
    await request('result',{id:result.command.id,session:result.command.session,result:value,error});}
}
$('pair').onclick=()=>{wake();return serial(async()=>{
  const base=serviceUrl($('base').value.trim()),old=state.base;state.base=base;
  try{const r=await request('pair',{code:$('code').value.trim().toUpperCase()},'');state={base,token:r.token};invalid=false;await save();$('code').value='';await stop();await poll();}
  catch(e){state.base=old;throw e;}
});};
$('bind').onclick=()=>{wake();return serial(async()=>{const [tab]=await chrome.tabs.query({active:true,currentWindow:true});if(!allowed(tab?.url))throw Error('请先选中一个B站首页、视频页或收藏页，再绑定');binding={tab_id:tab.id,url:tab.url,session:crypto.randomUUID(),control:false};await saveBinding();renderBinding();await poll();});};
$('control').onchange=()=>{wake();const control=$('control').checked;if(!binding){$('control').checked=false;$('notice').textContent='请先绑定B站标签页';return;}binding={...binding,session:crypto.randomUUID(),control};renderBinding();serial(async()=>{await saveBinding();await poll();});};
// 停止立即改变本地会话，正在等待的动作在返回前也会再次核对。
$('stop').onclick=()=>{wake();if(binding){binding={...binding,control:false,session:crypto.randomUUID()};renderBinding();}serial(async()=>{await saveBinding();await poll();});};
$('snapshot').onclick=()=>{wake();return serial(async()=>{if(!binding)throw Error('请先绑定B站标签页');const result=await site('snapshot',{},binding.session,false);$('debug-result').textContent=JSON.stringify(result,null,2);});};
$('open-workbench').onclick=()=>serial(async()=>{await chrome.tabs.create({url:serviceUrl($('base').value.trim())+'/connection'});});
async function start(){state=(await chrome.storage.local.get('connection')).connection||{base:'http://127.0.0.1:6861',token:''};binding=(await chrome.storage.session.get('binding')).binding||null;$('base').value=state.base;renderBinding();if(state.token)await serial(poll);else status('尚未配对');async function tick(){if(!ticking){ticking=true;await serial(()=>poll(15));ticking=false;}setTimeout(tick,(!state.token||invalid)?5000:offlineDelay===1000?0:offlineDelay);}tick();}
start().catch(e=>{$('notice').textContent=e.message;});
