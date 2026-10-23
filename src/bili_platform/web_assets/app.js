import {findMatchingCustomTagSuggestions, appendSuggestedCustomTag} from '/vendor/custom-tag-suggestions.js';

const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const esc = v => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => n==null?'':`${Math.floor(n/60)}:${String(Math.floor(n%60)).padStart(2,'0')}`;
const date = n => n ? new Date(n*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '尚未更新';
const coverUrl = raw => { const value=String(raw||'').replace(/^\/\//,'https://').replace(/^http:\/\//,'https://'); return /^https:\/\/[^/]+\.hdslb\.com\//.test(value)?value:''; };
const parseTags = value => [...new Map(value.split(/[,，\n]/).map(s=>s.trim()).filter(Boolean).map(s=>[s.toLocaleLowerCase(),s])).values()];
const studyNames = {inbox:'准备学习',learning:'正在学习',done:'已学习'};
let S, view='all', folder=null, tag=null, selected=new Set(), page=1, pageSize=24, currentVideo=null, detailRevision, originalAnnotation, dirty=false, pollTimer;

async function api(path, body, recover=true){
  const response=await fetch('/api/'+path, body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Library-Token':S?.token||''},body:JSON.stringify(body)});
  const value=await response.json();
  if(response.status===403&&recover&&['请先打开收藏库首页','缺少本地操作凭据'].includes(value.error)){
    // 服务重启会轮换本机会话；鉴权失败发生在写入前，可重新握手后安全重发一次。
    await fetch('/');
    const bootstrap=await (await fetch('/api/state')).json();
    if(bootstrap.token){if(S)S.token=bootstrap.token;else S=bootstrap;return api(path,body,false);}
  }
  if(!response.ok) {const e=new Error(value.error||'请求失败');e.status=response.status;throw e;}
  return value;
}
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,3600);}
function showError(error){$('#error-banner').textContent=error.message||String(error);$('#error-banner').hidden=false;}
async function refresh(){
  S=await api('state');$('#error-banner').hidden=true;
  $('#account-label').textContent=`本地账号 ${S.account||'未绑定'} · 只读连接B站`;
  selected=new Set([...selected].filter(b=>S.videos.some(v=>v.bvid===b)));
  render();renderJob(S.job);
}
function normalizedAnnotation(v){return {tags:v.annotation.tags||[],note:v.annotation.note||'',study:v.annotation.study||'inbox',archived:!!v.annotation.archived};}
function activeVideos(){return S.videos.filter(v=>!v.annotation.archived);}
function tagNames(){return [...new Set(S.videos.flatMap(v=>v.annotation.tags||[]))].sort((a,b)=>a.localeCompare(b,'zh-CN'));}
function setFilter(next){view=next.view??'all';folder=next.folder??null;tag=next.tag??null;page=1;render();}
function matches(v,q){
  if(!q)return true;
  // 沿用 BiliShelf 的字段式检索，并增加独立备注；普通查询同时命中标签和备注。
  const fields={标题:'title',title:'title',UP主:'author',up:'author',uploader:'author',简介:'description',description:'description',标签:'tags',customTag:'tags',自定义标签:'tags',备注:'note',note:'note'};
  let rest=q;
  for(const match of q.matchAll(/(?:^|\s)(标题|title|UP主|up|uploader|简介|description|标签|customTag|自定义标签|备注|note)[:：]([^:：]+?)(?=\s(?:标题|title|UP主|up|uploader|简介|description|标签|customTag|自定义标签|备注|note)[:：]|$)/gi)){
    const key=Object.keys(fields).find(k=>k.toLowerCase()===match[1].toLowerCase());
    const f=fields[key];const value=f==='tags'?v.annotation.tags.join(' '):f==='note'?v.annotation.note:v[f];
    if(!String(value||'').toLowerCase().includes(match[2].trim().toLowerCase()))return false;
    rest=rest.replace(match[0],' ');
  }
  let haystack=[v.title,v.author,v.description,v.annotation.note,...v.annotation.tags];
  if($('#search-transcript').checked)haystack.push(...(v.parts||[]).flatMap(p=>(p.segments||[]).map(s=>s.text)));
  const text=haystack.join(' ').toLowerCase();
  return rest.trim().toLowerCase().split(/\s+/).filter(Boolean).every(word=>text.includes(word));
}
function filtered(){
  const q=$('#search').value.trim();
  let list=S.videos.filter(v=>{
    const a=v.annotation;
    if(view==='archive'?!a.archived:a.archived)return false;
    if(view==='untagged'&&a.tags.length)return false;
    if(view==='notes'&&!a.note)return false;
    if(['learning','done'].includes(view)&&a.study!==view)return false;
    if(folder!=null&&!(v.folders||[]).includes(folder))return false;
    if(tag&&!a.tags.some(t=>t.toLowerCase()===tag.toLowerCase()))return false;
    return matches(v,q);
  });
  const sort=$('#sort').value;
  if(sort==='updated')list.sort((a,b)=>(b.annotation.updated||0)-(a.annotation.updated||0));
  if(sort==='title')list.sort((a,b)=>a.title.localeCompare(b.title,'zh-CN'));
  if(sort==='duration')list.sort((a,b)=>(b.duration||0)-(a.duration||0));
  return list;
}
function render(){
  const videos=activeVideos();
  const views=[['all','▦','所有收藏',videos.length],['untagged','○','尚未标注',videos.filter(v=>!v.annotation.tags.length).length],['learning','◷','正在学习',videos.filter(v=>v.annotation.study==='learning').length],['done','✓','已学习',videos.filter(v=>v.annotation.study==='done').length],['notes','≡','有私人备注',videos.filter(v=>v.annotation.note).length],['archive','▱','本地归档',S.videos.length-videos.length]];
  $('#views').innerHTML=views.map(([key,icon,name,count])=>`<button data-view="${key}" class="${view===key&&folder==null&&!tag?'active':''}"><span>${icon}　${name}</span><span>${count}</span></button>`).join('');
  $('#folders').innerHTML=S.sources.map(f=>`<button data-folder="${f.id}" class="${folder===f.id?'active':''}" title="${esc(f.complete?'已读取完整清单':'有界样本，不代表全夹')} "><span>▱　${esc(f.title)}</span><span>${videos.filter(v=>v.folders?.includes(f.id)).length}</span></button>`).join('')||'<span class="helper">暂未导入收藏夹</span>';
  $('#tag-list').innerHTML=tagNames().map(t=>`<button data-tag="${esc(t)}" class="${tag===t?'active':''}"><span>#　${esc(t)}</span><span>${videos.filter(v=>v.annotation.tags.includes(t)).length}</span></button>`).join('')||'<span class="helper">打开视频，添加你的第一个标签</span>';
  $('#overview').innerHTML=[['收藏',videos.length],['我的标签',tagNames().length],['有备注',videos.filter(v=>v.annotation.note).length]].map(([k,v])=>`<div class="stat"><strong>${v}</strong><small>${k}</small></div>`).join('');
  const title=tag?`# ${tag}`:folder!=null?S.sources.find(f=>f.id===folder)?.title:views.find(v=>v[0]===view)?.[2];
  $('#page-title').innerHTML=`${esc(title||'所有收藏')}<span class="title-dot">.</span>`;
  $('#page-description').textContent=view==='archive'?'这里只收起本地条目，原B站收藏保持原样；可随时恢复。':folder!=null?`${S.sources.find(f=>f.id===folder)?.complete?'完整导入':'当前为有界样本'} · 标签与备注不受来源文件夹限制。`:'记下为什么收藏，让下次想学的时候，找得到。';
  $('#undo-button').disabled=!S.history.some(h=>!h.undone);
  $('#sync-button').disabled=!S.sync_available||S.job?.status==='running';
  $('#clear-filters').hidden=!($('#search').value||tag||folder!=null||view!=='all');
  $('#active-filter').innerHTML=tag?`<span class="active-tag">标签：${esc(tag)} <button id="remove-tag-filter" aria-label="清除标签筛选">×</button></span>`:'';
  renderVideos();renderBatch();
}
function renderVideos(){
  const list=filtered(),pages=Math.max(1,Math.ceil(list.length/pageSize));page=Math.min(page,pages);
  $('#result-count').textContent=`${list.length} 条结果`;
  $('#video-grid').innerHTML=list.slice((page-1)*pageSize,page*pageSize).map(v=>{
    const a=v.annotation;const cover=coverUrl(v.cover);
    return `<article class="video-card ${selected.has(v.bvid)?'selected':''}" data-bvid="${esc(v.bvid)}"><div class="cover" data-open="${esc(v.bvid)}">${cover?`<img src="${esc(cover)}" loading="lazy" referrerpolicy="no-referrer" alt="${esc(v.title)}">`:`<div class="cover-placeholder">${esc(v.title.slice(0,2))}</div>`}<input class="select-video" type="checkbox" aria-label="选择 ${esc(v.title)}" data-select="${esc(v.bvid)}" ${selected.has(v.bvid)?'checked':''}><span class="study-badge">${studyNames[a.study]||'准备学习'}</span>${v.duration?`<span class="duration">${fmt(v.duration)}</span>`:''}</div><div class="card-body"><button class="video-title" data-open="${esc(v.bvid)}">${esc(v.title)}</button><p class="byline">◉　${esc(v.author||'元数据待更新')} <span>·</span> ${(v.parts||[]).some(p=>p.segments?.length)?'有字幕':'待理解内容'}</p><div class="tag-row">${a.tags.length?a.tags.slice(0,4).map(t=>`<button class="chip" data-tag="${esc(t)}">${esc(t)}</button>`).join(''):'<span class="chip muted">＋ 添加你的标签</span>'}${a.tags.length>4?`<span class="chip muted">+${a.tags.length-4}</span>`:''}</div><div class="card-foot"><span class="note-indicator" title="${esc(a.note)}">${a.note?'≡ '+esc(a.note):'还没有私人备注'}</span><button data-open="${esc(v.bvid)}">整理 ↗</button></div></div></article>`;
  }).join('')||`<div class="empty"><strong>${view==='archive'?'归档里还没有视频':'没有找到匹配的视频'}</strong>${view==='archive'?'在视频详情中可以将本地条目归档，B站收藏不受影响。':'换一个关键词，或清除筛选后再看看。'}<p><button id="empty-clear">查看所有收藏</button></p></div>`;
  $('#pagination').innerHTML=pages>1?`<button id="prev-page" ${page<=1?'disabled':''}>上一页</button><span>${page} / ${pages}</span><button id="next-page" ${page>=pages?'disabled':''}>下一页</button>`:'';
}
function renderBatch(){
  $('#batch-bar').hidden=selected.size===0;$('#batch-archive').textContent=view==='archive'?'恢复到书架':'本地归档';$('#selection-count').textContent=`已选 ${selected.size} 条`;
  $('#select-all').textContent=filtered().length&&filtered().every(v=>selected.has(v.bvid))?'取消当前结果选择':'选择当前结果';
}
function utility(title,body){$('#utility-content').innerHTML=`<div class="dialog-head"><h2>${esc(title)}</h2><button class="close-button" data-close="utility" aria-label="关闭">×</button></div><div class="utility-body">${body}<p class="dialog-error" id="utility-error"></p></div>`;if(!$('#utility-dialog').open)$('#utility-dialog').showModal();}
async function perform(fn){try{await fn();}catch(e){const el=$('#utility-dialog').open?$('#utility-error'):$('#detail-dialog').open?$('#detail-error'):null;if(el)el.textContent=e.message;else showError(e);}}
async function write(path,body,message){await api(path,{...body,revision:body.revision??S.revision});await refresh();toast(message);}
function openVideo(bvid){
  const v=S.videos.find(v=>v.bvid===bvid);if(!v)return;
  currentVideo=bvid;detailRevision=S.revision;originalAnnotation=JSON.stringify(normalizedAnnotation(v));dirty=false;
  const a=v.annotation;const j=v.judgment||{};const suggested=[...new Set([...(j.topics||[]),...(j.content_types||[]),...(j.formats||[])])].filter(t=>!a.tags.includes(t)).slice(0,8);
  const segments=(v.parts||[]).flatMap(p=>(p.segments||[]).map(s=>({...s,cid:p.cid})));
  $('#detail-content').innerHTML=`<div class="dialog-head"><h2>${esc(v.title)}</h2><button class="close-button" data-close="detail" aria-label="关闭视频详情">×</button></div><div class="detail-layout"><div class="detail-left"><div id="watch-area">${coverUrl(v.cover)?`<img class="detail-cover" src="${esc(coverUrl(v.cover))}" referrerpolicy="no-referrer" alt="${esc(v.title)}">`:'<div class="detail-cover cover-placeholder">拾藏</div>'}</div><div class="detail-actions"><button id="embed-play" class="primary">▶ 在这里观看</button><a href="https://www.bilibili.com/video/${encodeURIComponent(v.bvid)}" target="_blank" rel="noopener noreferrer">打开B站 ↗</a></div><p class="helper">站外播放由B站提供。若清晰度、登录或权限不适用，可打开原视频。${v.availability==='cached'?'当前元数据来自原始样本。':''}</p><span class="field-label">平台简介 · ${esc(v.author||'作者待更新')}</span><div class="description">${esc(v.description||'暂无简介')}</div><p class="helper">元数据更新：${date(v.synced)}　<span class="mono">${esc(v.bvid)}</span></p><details class="evidence-box"><summary>内容判断与已保存字幕 · ${segments.length?'可阅读':'尚无可靠字幕'}</summary><p>${esc(j.rationale||'尚未形成可靠内容判断。标题和简介不代表完整视频内容。')}</p><p class="helper">${j.coverage==='full'?'已取得全文字幕':j.coverage==='sampled'?'此前判断仅使用抽样字幕':'覆盖范围见实际字幕；画面信息可能未涵盖'} · 人工用途优先，内容标签需你确认。</p>${j.evidence?.length?j.evidence.map(e=>`<blockquote>${esc(e.quote||e.text||'')}</blockquote>`).join(''):''}<button id="fetch-subtitle" ${!S.sync_available?'disabled':''}>${segments.length?'重新读取字幕':'读取该视频字幕'}</button>${segments.length?`<p class="helper">点击时间可从对应位置播放。</p><div class="transcript-scroll">${segments.map(s=>`<div class="transcript-row"><button data-time="${s.start||0}" data-cid="${s.cid||''}">${fmt(s.start||0)}</button><span class="transcript-text">${esc(s.text)}</span></div>`).join('')}</div>`:'<p class="helper">只读取可核对身份的现成字幕，不启动下载、ASR或模型。</p>'}</details></div><div class="detail-right"><label class="field-label" for="detail-tags">我的标签</label><input id="detail-tags" value="${esc(a.tags.join(', '))}" placeholder="例如 示例技能, 构图参考" autocomplete="off"><div class="suggestions" id="tag-suggestions"></div><p class="helper">逗号分隔；可自由新增，也可复用已有标签。</p>${suggested.length?`<span class="field-label">已有内容线索 · 点击才加入</span><div class="suggestions">${suggested.map(t=>`<button class="chip" data-suggest="${esc(t)}">＋ ${esc(t)}</button>`).join('')}</div><p class="helper">来自已保存内容判断，不等于你的收藏目的。</p>`:''}<label class="field-label" for="detail-note">为什么收藏 / 私人备注</label><textarea id="detail-note" placeholder="这条对我有什么用？准备什么时候回来看？">${esc(a.note)}</textarea><p class="helper">备注独立保存，同步平台简介不会覆盖它。</p><label class="field-label" for="detail-study">学习进度</label><select id="detail-study">${Object.entries(studyNames).map(([k,n])=>`<option value="${k}" ${a.study===k?'selected':''}>${n}</option>`).join('')}</select><div class="save-row"><span id="save-state" class="small-status">已保存的标注</span><button id="save-detail" class="primary">保存标注</button></div><p class="dialog-error" id="detail-error"></p><hr><div class="save-row"><span class="helper">只影响本地展示，B站收藏不变</span><button id="archive-detail">${a.archived?'恢复到书架':'本地归档'}</button></div></div></div>`;
  try{const draft=JSON.parse(sessionStorage.getItem('draft:'+bvid)||'null');if(draft){$('#detail-tags').value=draft.tags;$('#detail-note').value=draft.note;$('#detail-study').value=draft.study;dirty=true;$('#save-state').textContent='已恢复未保存草稿';$('#save-state').className='unsaved';if(draft.original!==originalAnnotation)$('#detail-error').textContent='保存内容已变化；请核对当前草稿再决定是否保存。';}}catch{}
  if(!$('#detail-dialog').open)$('#detail-dialog').showModal();suggestTags();
}
function suggestTags(){const input=$('#detail-tags');if(!input)return;$('#tag-suggestions').innerHTML=findMatchingCustomTagSuggestions(tagNames(),input.value.replaceAll('，',','),6).map(t=>`<button class="chip" data-suggest="${esc(t)}">${esc(t)}</button>`).join('');}
function markDirty(){dirty=true;$('#save-state').textContent='有未保存的修改';$('#save-state').className='unsaved';try{sessionStorage.setItem('draft:'+currentVideo,JSON.stringify({tags:$('#detail-tags').value,note:$('#detail-note').value,study:$('#detail-study').value,original:originalAnnotation}));}catch{}}
async function saveDetail(){
  const button=$('#save-detail');button.disabled=true;
  try{await write('edit',{revision:detailRevision,bvids:[currentVideo],patch:{tags:parseTags($('#detail-tags').value),note:$('#detail-note').value,study:$('#detail-study').value}},'标注已保存');dirty=false;sessionStorage.removeItem('draft:'+currentVideo);detailRevision=S.revision;originalAnnotation=JSON.stringify(normalizedAnnotation(S.videos.find(v=>v.bvid===currentVideo)));$('#save-state').textContent='已保存到本机';$('#save-state').className='small-status';$('#detail-error').textContent='';}
  catch(e){if(e.status===409){await refresh();const current=JSON.stringify(normalizedAnnotation(S.videos.find(v=>v.bvid===currentVideo)));if(current===originalAnnotation){detailRevision=S.revision;e.message='后台元数据已更新，当前输入保留。再次点击保存即可。';}else e.message+='。请保留当前输入，并重新打开详情对比已保存标注。';}$('#detail-error').textContent=e.message;}
  finally{button.disabled=false;}
}
function closeDetail(){if(dirty){utility('还有未保存的标注','<p>可以继续编辑，或明确放弃这次草稿。刷新页面也会保留当前会话的未保存草稿。</p><button id="keep-editing" class="primary wide">继续编辑</button><button id="discard-draft" class="wide">放弃草稿并关闭</button>');return;}$('#detail-dialog').close();$('#watch-area')?.replaceChildren();dirty=false;}
function play(time=0,cid=''){$('#watch-area').innerHTML=`<iframe title="B站视频播放器" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen src="https://player.bilibili.com/player.html?bvid=${encodeURIComponent(currentVideo)}&autoplay=1&danmaku=0&t=${Number(time)||0}${cid?'&cid='+encodeURIComponent(cid):''}"></iframe>`;}
function renderJob(job){
  $('#job-panel').hidden=!job;if(!job)return;
  const labels={running:'正在只读更新',complete:'更新完成',partial:'更新完成，部分条目需重试',failed:'更新已停止',cancelled:'已停止更新',interrupted:'上次更新被中断'};
  $('#job-panel').innerHTML=`<div class="job-top"><strong>${labels[job.status]||job.status} · ${job.done||0}/${job.total||0}</strong>${job.status==='running'?'<button id="cancel-sync" class="quiet">停止</button>':'<button id="dismiss-job" class="text-button">收起</button>'}</div><progress value="${job.done||0}" max="${job.total||1}"></progress><div>${esc(job.status==='running'?job.message:job.error||'手工标签和备注保持独立，未修改B站收藏。')}</div>${job.errors?.length?`<details><summary>${job.errors.length} 条读取异常</summary>${job.errors.map(e=>`<p>${esc(e.bvid)}：${esc(e.message)}</p>`).join('')}</details>`:''}`;
  if(job.status==='running'&&!pollTimer)pollTimer=setTimeout(pollJob,1200);
}
async function pollJob(){pollTimer=null;try{const job=await api('job');renderJob(job);if(job?.status!=='running'){await refresh();toast(job?.status==='complete'?'更新完成，私人标注保留':'更新已停止，请查看任务状态');}}catch(e){showError(e);}}
async function startSync(body){const result=await api('sync',body);S.job=result.result;renderJob(S.job);$('#sync-button').disabled=true;}
async function backupDialog(){
  utility('备份与恢复',`<p>备份包含视频信息、来源、标签、私人备注、学习进度和已保存字幕。不含B站登录凭证；它不包含视频文件。</p><a class="primary wide" href="/api/backup" download>↓ 下载完整 JSON 备份</a><h3>从备份恢复</h3><input type="file" id="backup-file" accept="application/json,.json" aria-label="选择备份文件"><p class="helper">也可粘贴备份内容，方便在当前浏览器恢复。</p><textarea id="backup-json" aria-label="备份JSON" placeholder="粘贴本工具导出的 JSON 备份"></textarea><label class="field-label" for="restore-mode">同一视频的标注如何处理</label><select id="restore-mode"><option value="merge">合并标签，保留本机已有备注</option><option value="overwrite">用备份覆盖同视频的标注</option></select><p class="helper">只接受同账号备份。覆盖前自动保存本机备份，恢复标注后还可撤销。</p><button id="preview-restore" class="wide">检查并预览恢复</button><div id="restore-preview"></div><button id="confirm-restore" class="primary wide" hidden>确认恢复到本机</button>`);
  const first=$('#utility-content .utility-body');
  const block=document.createElement('section');block.id='local-backups';
  block.innerHTML='<h3>工具内的本机备份</h3><p>文件保留在本机数据目录，直接选择恢复，无需浏览器上传。</p><button id="save-local-backup" class="wide">保存一份本机备份</button><label class="field-label" for="local-backup-select">选择已保存备份</label><select id="local-backup-select" class="wide"></select><button id="preview-local-restore" class="wide">预览本机备份恢复</button>';
  first.insertBefore(block,first.querySelector('h3'));
  await refreshLocalBackups();
}
async function refreshLocalBackups(){const list=await api('backups');$('#local-backup-select').innerHTML=list.map(b=>`<option value="${esc(b.id)}">${date(b.created)} · ${b.automatic?'恢复前自动备份':'手动备份'} · ${Math.ceil(b.size/1024)}KB</option>`).join('')||'<option value="">尚无本机备份</option>';$('#preview-local-restore').disabled=!list.length;$('#confirm-restore').hidden=true;$('#restore-preview').innerHTML='';}
function tagsDialog(){utility('管理我的标签',`<p>重命名会同步更新本地库中所有关联视频；同名标签会合并。移除标签不删除视频或备注，之后可以撤销。</p>${tagNames().map(t=>`<div class="tag-manage-row"><span># ${esc(t)}</span><button data-rename-tag="${esc(t)}">重命名</button><button data-remove-tag="${esc(t)}">移除</button></div>`).join('')||'<p>还没有标签。打开视频详情即可添加。</p>'}`);}
async function importDialog(){
  utility('导入B站收藏夹','<p>正在核对登录账号并读取收藏夹列表…</p>');
  const folders=await api('platform/folders');
  utility('导入B站收藏夹',`<p>只读导入到本地，不创建、删除或移动任何站内收藏。初次建议先读取最近20条。</p><label class="field-label" for="import-folder">选择收藏夹</label><select id="import-folder" class="wide">${folders.map(f=>`<option value="${f.id}">${esc(f.title)} · ${f.count??'?'}条</option>`).join('')}</select><label class="field-label" for="import-limit">本次读取范围</label><select id="import-limit" class="wide"><option value="20">最近20条</option><option value="50">最近50条</option><option value="100">最近100条</option><option value="0">整个收藏夹（可能耗时较长）</option></select><p class="helper">先读取清单，再补充视频元数据；不自动调用模型或读取全夹字幕。过程中可停止，已保存标注保留。</p><button id="confirm-import" class="primary wide" ${!folders.length?'disabled':''}>开始只读导入</button>`);
}
document.addEventListener('click',event=>perform(async()=>{
  const b=event.target.closest('button,a');
  if(event.target.closest('[data-select]'))return;
  if(b?.dataset.view){setFilter({view:b.dataset.view});return;}
  if(b?.dataset.folder){setFilter({folder:Number(b.dataset.folder)});return;}
  if(b?.dataset.tag){setFilter({tag:b.dataset.tag});return;}
  if(b?.dataset.open){openVideo(b.dataset.open);return;}
  if(!b&&event.target.closest('[data-open]')){openVideo(event.target.closest('[data-open]').dataset.open);return;}
  if(b?.dataset.close){if(b.dataset.close==='detail')closeDetail();else $('#utility-dialog').close();return;}
  if(b?.dataset.suggest){$('#detail-tags').value=appendSuggestedCustomTag($('#detail-tags').value.replaceAll('，',','),b.dataset.suggest);markDirty();suggestTags();return;}
  if(b?.dataset.time!==undefined){play(b.dataset.time,b.dataset.cid);return;}
  if(b?.dataset.renameTag){const old=b.dataset.renameTag;utility('重命名标签',`<p>当前标签：${esc(old)}</p><input id="tag-new-name" class="wide" aria-label="新的标签名" value="${esc(old)}"><button id="confirm-rename" class="primary wide" data-old="${esc(old)}">保存新名称</button>`);return;}
  if(b?.dataset.removeTag){const old=b.dataset.removeTag;utility('移除标签',`<p>从所有本地视频移除「${esc(old)}」，视频和私人备注保留。可用顶部撤销恢复。</p><button id="confirm-remove-tag" class="wide" data-old="${esc(old)}">确认移除标签</button>`);return;}
  switch(b?.id){
    case 'clear-filters':case 'empty-clear':$('#search').value='';setFilter({});break;
    case 'remove-tag-filter':tag=null;page=1;render();break;
    case 'select-all':{const list=filtered();if(list.every(v=>selected.has(v.bvid)))list.forEach(v=>selected.delete(v.bvid));else list.forEach(v=>selected.add(v.bvid));renderVideos();renderBatch();break;}
    case 'clear-selection':selected.clear();renderVideos();renderBatch();break;
    case 'batch-add':case 'batch-remove':await write('edit',{bvids:[...selected],patch:{[b.id==='batch-add'?'add_tags':'remove_tags']:parseTags($('#batch-tags').value)}},'批量标签已保存');$('#batch-tags').value='';break;
    case 'batch-archive':await write('edit',{bvids:[...selected],patch:{archived:view!=='archive'}},view==='archive'?'已恢复到书架':'已归档到本机，可撤销');selected.clear();render();break;
    case 'save-detail':await saveDetail();break;
    case 'keep-editing':$('#utility-dialog').close();break;
    case 'discard-draft':sessionStorage.removeItem('draft:'+currentVideo);dirty=false;$('#utility-dialog').close();$('#detail-dialog').close();$('#watch-area')?.replaceChildren();break;
    case 'archive-detail':if(dirty){$('#detail-error').textContent='请先保存或放弃当前修改，再归档。';break;}await write('edit',{bvids:[currentVideo],patch:{archived:!S.videos.find(v=>v.bvid===currentVideo).annotation.archived}},'本地展示已更新');$('#detail-dialog').close();break;
    case 'embed-play':play();break;
    case 'fetch-subtitle':await startSync({kind:'transcript',bvids:[currentVideo]});toast('已开始读取字幕；完成后重新打开详情可查看');break;
    case 'undo-button':await write('undo',{},'已撤销上一次标注操作');break;
    case 'backup-button':await backupDialog();break;
    case 'save-local-backup':await api('backup/local',{});await refreshLocalBackups();toast('本机备份已保存');break;
    case 'preview-local-restore':{const backup_id=$('#local-backup-select').value;const mode=$('#restore-mode').value;const result=(await api('restore/local/preview',{backup_id,mode})).result;$('#restore-preview').innerHTML=`<div class="confirm-preview">本机备份 ${result.videos} 条 · 新增 ${result.new} 条 · 标注不同 ${result.conflicts} 条<br>${mode==='merge'?'合并标签，保留本机已有备注。':'以备份恢复同视频标注。'}不修改B站。</div>`;const button=$('#confirm-restore');button.hidden=false;button.dataset.revision=S.revision;button.dataset.backupId=backup_id;break;}
    case 'manage-tags':tagsDialog();break;
    case 'confirm-rename':await write('tag',{old:b.dataset.old,new:$('#tag-new-name').value},'标签已更新');tagsDialog();break;
    case 'confirm-remove-tag':await write('tag',{old:b.dataset.old},'标签已移除，可撤销');tag=null;tagsDialog();break;
    case 'sync-button':await startSync({kind:'metadata',...(selected.size?{bvids:[...selected]}:{})});break;
    case 'cancel-sync':await api('sync/cancel',{});toast('将在当前请求结束后停止');break;
    case 'dismiss-job':$('#job-panel').hidden=true;break;
    case 'import-button':await importDialog();break;
    case 'confirm-import':await startSync({kind:'import',folder_id:Number($('#import-folder').value),limit:Number($('#import-limit').value)});$('#utility-dialog').close();break;
    case 'preview-restore':{const backup=JSON.parse($('#backup-json').value);const mode=$('#restore-mode').value;const result=(await api('restore/preview',{backup,mode})).result;$('#restore-preview').innerHTML=`<div class="confirm-preview">备份 ${result.videos} 条 · 新增 ${result.new} 条 · 标注不同 ${result.conflicts} 条<br>${mode==='merge'?'合并标签，保留本机已有备注。':'同视频标注将使用备份内容。'}不修改B站。</div>`;$('#confirm-restore').hidden=false;$('#confirm-restore').dataset.revision=S.revision;delete $('#confirm-restore').dataset.backupId;break;}
    case 'confirm-restore':await write(b.dataset.backupId?'restore/local':'restore',{...(b.dataset.backupId?{backup_id:b.dataset.backupId}:{backup:JSON.parse($('#backup-json').value)}),mode:$('#restore-mode').value,revision:Number(b.dataset.revision)},'备份恢复完成');$('#utility-dialog').close();break;
    case 'prev-page':page--;renderVideos();break;
    case 'next-page':page++;renderVideos();break;
    case 'about-button':utility('关于拾藏',`<div class="about-columns">这是你的本地收藏检索与学习书架。<br>① 保留原B站收藏位置，用多个标签找到相关视频。<br>② 用私人备注记录收藏原因，平台简介更新不会覆盖它。<br>③ 搜索标题、标签、备注；勾选后也搜索已保存字幕。支持「标签:示例技能」「备注:创业」等字段。<br>④ 已有内容判断提供标签线索，点击后才加入。不会自动调用模型。<br>⑤ 本地归档和撤销不改变B站收藏。<br>⑥ 用JSON备份迁移；备份包含私人文字，请自行保管。<br>默认从空库开始；导入范围在左侧收藏夹中显示。<br>底座：bilibili-platform；标签补全复用BiliShelf（MIT），其余适配与私人备注由本项目维护。</div>`);break;
  }
}));
document.addEventListener('change',event=>perform(async()=>{
  if(event.target.dataset.select){event.stopPropagation();const v=event.target.dataset.select;if(event.target.checked)selected.add(v);else selected.delete(v);renderVideos();renderBatch();}
  if(event.target.id==='sort'||event.target.id==='search-transcript'){page=1;renderVideos();}
  if(event.target.id==='detail-study')markDirty();
  if(event.target.id==='batch-study'&&event.target.value){await write('edit',{bvids:[...selected],patch:{study:event.target.value}},'学习状态已保存');event.target.value='';}
  if(event.target.id==='backup-file'){const file=event.target.files[0];if(file){if(file.size>30_000_000)throw Error('备份文件超过30MB');$('#backup-json').value=await file.text();$('#confirm-restore').hidden=true;$('#restore-preview').innerHTML='';}}
  if(event.target.id==='restore-mode'||event.target.id==='local-backup-select'){$('#confirm-restore').hidden=true;$('#restore-preview').innerHTML='';}
}));
document.addEventListener('input',event=>{
  if(event.target.id==='search'){page=1;renderVideos();$('#clear-filters').hidden=false;}
  if(event.target.id==='detail-tags'){markDirty();suggestTags();}
  if(event.target.id==='detail-note')markDirty();
  if(event.target.id==='backup-json'){$('#confirm-restore').hidden=true;$('#restore-preview').innerHTML='';}
});
$('#detail-dialog').addEventListener('cancel',event=>{event.preventDefault();closeDetail();});
$('#detail-dialog').addEventListener('close',()=>$('#watch-area')?.replaceChildren());
document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)&&!$('dialog[open]')){event.preventDefault();$('#search').focus();}if((event.ctrlKey||event.metaKey)&&event.key==='Enter'&&$('#detail-dialog').open){event.preventDefault();perform(saveDetail);}});
refresh().catch(showError);
