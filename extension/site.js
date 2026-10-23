(() => {
  if (globalThis.biliWorkbenchRun) return;
  let refs=new Map(), pageKey='';
  function allowed(raw) {
    const u=new URL(raw,location.href);
    if(u.protocol!=='https:' || u.username || u.password || (u.port && u.port!=='443')) return false;
    return (u.hostname==='www.bilibili.com' && (u.pathname==='/' || /^\/video\/BV[A-Za-z0-9]{10}\/?$/.test(u.pathname))) || (u.hostname==='space.bilibili.com' && /^\/\d+\/favlist\/?$/.test(u.pathname));
  }
  function snapshot() {
    if(!allowed(location.href)) throw Error('当前页面不在允许的B站范围');
    pageKey=crypto.randomUUID(); refs=new Map();
    const links=[];
    for(const a of document.querySelectorAll('a[href]')) {
      if(links.length>=40) break;
      const rect=a.getBoundingClientRect(), title=(a.innerText||a.getAttribute('title')||'').trim();
      if(!title || rect.width===0 || rect.height===0 || rect.bottom<0 || rect.top>innerHeight || !allowed(a.href)) continue;
      const ref='link-'+links.length;refs.set(ref,{element:a,url:a.href,page:location.href}); links.push({ref,title:title.slice(0,150),url:a.href});
    }
    const video=document.querySelector('video,bwp-video');
    return {url:location.href,title:document.title,bvid:location.pathname.match(/^\/video\/(BV[A-Za-z0-9]{10})/)?.[1]||null,
      heading:document.querySelector('h1')?.innerText||'',ready:document.readyState,
      text:document.body.innerText.slice(0,6000),scroll_y:scrollY,page_key:pageKey,links,
      player:video?{paused:video.paused,current_time:Number(video.currentTime)||0,duration:Number.isFinite(video.duration)?video.duration:null}:null};
  }
  globalThis.biliWorkbenchRun=async(action,args)=>{
    if(!allowed(location.href)) throw Error('绑定页面已离开允许的B站范围');
    if(action==='snapshot') return {page:snapshot()};
    if(action==='scroll') {const before=scrollY;window.scrollBy({top:args.y,behavior:'instant'});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return {before_y:before,after_y:scrollY,page:snapshot()};}
    if(action==='click') {
      const record=refs.get(args.ref);
      if(args.page_key!==pageKey || !record || record.page!==location.href || !record.element.isConnected || record.element.href!==record.url || !allowed(record.url)) throw Error('链接快照已失效，请重新读取页面');
      return {navigate_to:record.url};
    }
    if(action==='player') {
      const video=document.querySelector('video,bwp-video');
      if(!video) throw Error('当前页面没有可控制的视频播放器');
      if(args.operation==='play') await video.play();
      else if(args.operation==='pause') video.pause();
      else if(args.operation==='seek') {if(!Number.isFinite(args.seconds))throw Error('跳转需要秒数');video.currentTime=args.seconds;}
      else throw Error('播放器操作无效');
      return {page:snapshot()};
    }
    throw Error('未开放此页面操作');
  };
})();
