export function serviceUrl(raw) {
  const u = new URL(raw);
  if (u.protocol !== 'http:' || u.hostname !== '127.0.0.1' || u.username || u.password || u.search || u.hash || u.pathname !== '/') throw Error('只允许 http://127.0.0.1:端口 形式的本机工作台地址');
  return u.origin;
}
export function currentPage(tab) {
  if (!tab?.url) throw Error('请在当前B站页面点击浏览器工具栏中的扩展图标，再发送页面');
  const u = new URL(tab.url);
  if (u.protocol !== 'https:' || !['www.bilibili.com','space.bilibili.com'].includes(u.hostname) || u.username || u.password || (u.port && u.port !== '443')) throw Error('请在B站视频页或收藏页点击扩展图标');
  const match = u.hostname === 'www.bilibili.com' && u.pathname.match(/^\/video\/(BV[a-zA-Z0-9]{10})\/?$/);
  if (!match && !(u.hostname === 'space.bilibili.com' && /^\/\d+\/favlist\/?$/.test(u.pathname))) throw Error('只发送B站视频页或收藏页');
  let url = u.origin + u.pathname;
  if (!match && /^\d+$/.test(u.searchParams.get('fid') || '')) url += '?fid=' + u.searchParams.get('fid');
  return {url, title:String(tab.title || '').slice(0,500), bvid:match ? match[1] : null};
}
