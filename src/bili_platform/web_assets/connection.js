const $=id=>document.getElementById(id);
let csrf='',S=null,plan=null,loadedSource=null,videoRows=[],busy=false,refreshing=null,jobsSignature='',readingPacket=null;
$('service-url').textContent=location.origin;
async function api(path,body,retry=true){
 const r=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-Library-Token':csrf},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(8000)});
 const v=await r.json();if(r.status===403&&retry){await fetch('/connection');const s=await api('runtime/state',undefined,false);csrf=s.token;return api(path,body,false);}if(!r.ok)throw Error(v.error||'请求失败');return v;
}
function show(value){return JSON.stringify(value,null,2);}
function notice(value){$('notice').textContent=value;}
function render(s){
 S=s;csrf=s.token;
 const b=s.bridge.browser;
 $('connection-status').textContent=!s.bridge.paired?'扩展尚未配对':s.bridge.online?'扩展在线':'已配对 · 扩展侧栏离线';
 $('connection-status').dataset.online=String(s.bridge.online);
 $('browser-binding').textContent=b?`标签页 ${b.tab_id} · ${b.control?'允许操作':'由你操作'} · ${b.url}`:'请在Chrome扩展侧栏绑定一个B站标签页';
 if(!s.session_configured)$('account-state').textContent='后台尚未配置B站会话；浏览器操作仍可独立使用。';
 $('action-catalog').textContent=s.catalog.actions.map(a=>a.name+' · '+a.description).join('\n');
 const signature=JSON.stringify(s.jobs);if(signature===jobsSignature)return;jobsSignature=signature;
 const jobs=$('jobs'),open=new Set([...jobs.querySelectorAll('details[open]')].map(e=>e.dataset.jobId));jobs.replaceChildren();
 for(const j of s.jobs.slice(0,10)){
   const box=document.createElement('details'),summary=document.createElement('summary');
   box.dataset.jobId=j.id;box.open=open.has(j.id);
   summary.textContent=`${j.action} · ${({queued:'排队',running:'执行中',complete:'已返回',failed:'失败',cancelled:'已取消',interrupted:'中断'})[j.status]||j.status}${j.result?.status?' · '+j.result.status:''}`;
   const pre=document.createElement('pre');pre.textContent=show(j.error||j.result||j.arguments);
   box.append(summary,pre);
   if(['queued','running'].includes(j.status)){const cancel=document.createElement('button');cancel.textContent='取消后续步骤';cancel.onclick=()=>perform(async()=>{await api('runtime/cancel',{job_id:j.id});});box.append(cancel);}
   jobs.append(box);
 }
}
async function refresh(){if(refreshing)return refreshing;refreshing=(async()=>{try{const s=await api('runtime/state');render(s);return s;}catch(e){$('connection-status').textContent='后台离线 · 等待自动重连';$('connection-status').dataset.online='false';throw e;}finally{refreshing=null;}})();return refreshing;}
async function invoke(action,args={}){
 const started=performance.now();
 const {result:r}=await api('runtime/call',{action,args,request_id:crypto.randomUUID()});
 notice('正在执行 '+action+'；可在任务区查看进度或取消。');
 await refresh();
 for(;;){const j=await api('runtime/job?id='+encodeURIComponent(r.job_id));
  if(S)render({...S,jobs:[j,...S.jobs.filter(x=>x.id!==j.id)]});
  if(!['queued','running'].includes(j.status)){
   if(j.status!=='complete')throw Error(j.error?.message||'任务未完成');
   notice((j.result?.status==='partial'?'部分完成，请查看实际结果；未自动重试写入。':'已收到实际执行结果。')+' 耗时 '+((performance.now()-started)/1000).toFixed(1)+' 秒');
   return {...j.result,_job_id:j.id};
  }
  await new Promise(resolve=>setTimeout(resolve,500));
 }
}
async function perform(fn){if(busy){notice('已有操作正在执行，可在任务区取消后续步骤。');return;}busy=true;try{await fn();}catch(e){notice(e.message);}finally{busy=false;}}
function invalidate(){plan=null;$('execute-favorites').disabled=true;$('favorites-preview').textContent='选择已变化，请重新预览。';}
async function browser(action,args={}){
 const r=await invoke('browser.'+action,args);$('browser-result').textContent=show(r);$('browser-summary').textContent=[r.page.heading||r.page.title,r.page.bvid,r.page.player?'播放位置 '+r.page.player.current_time.toFixed(1)+'秒 · '+(r.page.player.paused?'已暂停':'播放中'):'',r.after_y!==undefined?'页面位置 '+r.after_y:''].filter(Boolean).join(' · ');
 const links=$('browser-links');links.replaceChildren();
 for(const l of r.page?.links||[]){const button=document.createElement('button');button.className='secondary';button.textContent=l.title;button.onclick=()=>perform(()=>browser('click',{ref:l.ref,page_key:r.page.page_key}));links.append(button);}
}
$('generate-code').onclick=()=>perform(async()=>{const {result:r}=await api('extension/code',{});$('pairing-code').textContent=r.code;});
$('revoke').onclick=()=>perform(async()=>{await api('extension/revoke',{});$('pairing-code').textContent='';await refresh();});
$('stop-browser').onclick=async()=>{try{await api('runtime/stop-browser',{});notice('已停止后续浏览器操作。');await refresh();}catch(e){notice(e.message);}};
$('browser-snapshot').onclick=()=>perform(()=>browser('snapshot'));
$('browser-navigate').onclick=()=>perform(()=>browser('navigate',{url:$('navigate-url').value.trim()}));
$('scroll-up').onclick=()=>perform(()=>browser('scroll',{y:-800}));
$('scroll-down').onclick=()=>perform(()=>browser('scroll',{y:800}));
$('player-pause').onclick=()=>perform(()=>browser('player',{operation:'pause'}));
$('player-play').onclick=()=>perform(()=>browser('player',{operation:'play'}));
$('player-seek').onclick=()=>perform(()=>browser('player',{operation:'seek',seconds:Number($('seek-time').value)}));
async function loadFolders(){
 const r=await invoke('favorites.folders');$('account-state').textContent='已核对后台B站账号 UID '+r.account_mid;
 for(const id of ['source-folder','target-folder']){const select=$(id),old=select.value;select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='请选择收藏夹';select.append(empty);for(const f of r.folders){const o=document.createElement('option');o.value=f.id;o.textContent=f.title+' · '+(f.count??'?')+'条';select.append(o);}select.value=old;}
 invalidate();
}
$('load-folders').onclick=()=>perform(loadFolders);
$('create-folder-form').onsubmit=event=>{event.preventDefault();perform(async()=>{const title=$('folder-title').value.trim();const r=await invoke('favorites.create',{title,confirmation:title});notice('已创建并读回私密收藏夹：'+r.folder.title+' · ID '+r.folder.id);$('folder-title').value='';await loadFolders();});};
$('load-videos').onclick=()=>perform(async()=>{
 const id=Number($('source-folder').value);if(!id)throw Error('请选择来源收藏夹');
 const r=await invoke('favorites.videos',{folder_id:id,limit:Number($('video-limit').value)});
 loadedSource=id;videoRows=r.videos;invalidate();$('video-scope').textContent=`${r.folder.title} · 本次${r.videos.length}条 · ${r.complete?'已读完整':'有界范围，非全夹'}`;
 $('favorite-videos').replaceChildren();
 for(const v of r.videos){if(!v.bvid||v.type!==2)continue;const label=document.createElement('label');label.className='video-option';const input=document.createElement('input');input.type='checkbox';input.value=v.bvid;input.onchange=invalidate;label.append(input,document.createTextNode(' '+v.title+' · '+v.bvid));$('favorite-videos').append(label);}
});
for(const id of ['source-folder','target-folder','operation-mode'])$(id).onchange=()=>{invalidate();if(id==='source-folder'){$('favorite-videos').replaceChildren();videoRows=[];loadedSource=null;}};
$('prepare-favorites').onclick=()=>perform(async()=>{
 const source_id=Number($('source-folder').value),target_id=Number($('target-folder').value),bvids=[...$('favorite-videos').querySelectorAll('input:checked')].map(i=>i.value);
 if(!source_id||!target_id||loadedSource!==source_id||!bvids.length)throw Error('请选择来源、目标和来源中的视频');
 const r=await invoke('favorites.prepare',{source_id,target_id,mode:$('operation-mode').value,bvids});
 plan=r;$('favorites-summary').textContent=(r.mode==='move'?'移动':'复制')+' '+r.items.length+'条：'+r.source.title+' → '+r.target.title;$('favorites-preview').textContent=show(r);$('execute-favorites').disabled=false;
});
$('execute-favorites').onclick=()=>perform(async()=>{if(!plan)throw Error('请先生成预览');const saved=plan;invalidate();const r=await invoke('favorites.execute',{plan_id:saved._job_id,confirmation:saved.confirmation});$('favorites-preview').textContent=show(r);$('favorites-summary').textContent=(r.status==='verified'?'已读回核对':'请核对部分结果')+'：'+r.source.title+' → '+r.target.title;});
async function readVideo(){
 const text=$('reading-times').value.trim(),args={bvid:$('reading-bvid').value.trim(),part:Number($('reading-part').value)};
 if(text)args.times=text.split(/[,，\s]+/).map(Number);
 const r=await invoke('video.read',args);readingPacket=r;
 $('reading-summary').textContent=r.title+' · 第'+r.coverage.part+'/'+r.coverage.parts_total+'P · '+r.frames.length+'张抽样画面 · '+(r.segments.length?'字幕'+r.segments.length+'行':'没有可用字幕')+(r.cached?' · 复用已保存材料':'');
 const subtitleRange=r.segments.length?'字幕覆盖返回时段 '+r.segments[0].start+'–'+r.segments.at(-1).end+'秒 / 视频 '+r.coverage.duration_seconds+'秒。':'';
 $('reading-coverage').textContent=subtitleRange+(r.coverage.subtitle_error?'字幕被拒用：'+r.coverage.subtitle_error+'。':'')+'画面为抽样；声音未独立分析。可指定秒数补取画面；未读取的分P不在范围内。材料已可供当前会话或其他阅读器使用。';
 $('reading-gallery').replaceChildren();
 for(const f of r.frames){const figure=document.createElement('figure'),img=document.createElement('img'),caption=document.createElement('figcaption');img.src=f.url;img.alt=r.title+' · '+f.seconds+'秒';img.loading='lazy';caption.textContent=f.id+' · '+f.seconds+'秒';const full=document.createElement('a');full.href=f.url;full.target='_blank';full.rel='noopener';full.title='打开原尺寸画面';full.append(img);figure.append(full,caption);$('reading-gallery').append(figure);}
 $('reading-text').textContent=r.segments.map(s=>'['+s.start+'–'+s.end+'] '+s.text).join('\n')||'没有可用字幕；不能据画面推断未听到的讲话。';
 $('reading-manifest').textContent=r.manifest;
 $('reading-analysis').textContent=r.reading?r.reading.summary:'尚未保存模型阅读结果；当前完成的是材料获取。';
}
$('read-video').onclick=()=>perform(readVideo);
$('read-current-video').onclick=()=>perform(async()=>{const bvid=(S?.bridge.browser?.url||'').match(/\/video\/(BV[A-Za-z0-9]{10})/);if(!bvid)throw Error('请先在扩展绑定一个B站视频页');$('reading-bvid').value=bvid[1];await readVideo();});
async function watch(){try{if(!busy)await refresh();}catch{}setTimeout(watch,busy?1000:document.visibilityState==='hidden'?30000:10000);}
refresh().catch(e=>notice(e.message));watch();
