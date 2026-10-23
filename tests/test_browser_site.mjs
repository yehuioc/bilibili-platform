import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';

const source=await readFile(new URL('../extension/site.js',import.meta.url),'utf8');
function fixture(){
 const anchor={href:'https://www.bilibili.com/video/BV1AbCdEfGhJ',innerText:'进入视频',isConnected:true,getBoundingClientRect:()=>({width:100,height:20,top:5,bottom:25})};
 const player={paused:true,currentTime:0,duration:120,play:async()=>{player.paused=false;},pause:()=>{player.paused=true;}};
 const ctx={URL,Map,Error,Number,Promise,crypto:{randomUUID},innerHeight:800,scrollY:0,location:{href:'https://www.bilibili.com/video/BV1AbCdEfGhJ',pathname:'/video/BV1AbCdEfGhJ'},
   requestAnimationFrame:fn=>fn(),document:{title:'真实页面',readyState:'complete',body:{innerText:'当前正文'},querySelectorAll:()=>[anchor],querySelector:s=>s==='h1'?{innerText:'视频标题'}:player}};
 ctx.window={scrollBy:({top})=>{ctx.scrollY=Math.max(0,ctx.scrollY+top);}};
 vm.createContext(ctx);vm.runInContext(source,ctx);return {ctx,anchor,player,run:ctx.biliWorkbenchRun};
}
test('snapshot returns actual page and safe navigation refs; stale refs rejected',async()=>{
 const {run,anchor}=fixture();const first=(await run('snapshot',{})).page;
 assert.equal(first.bvid,'BV1AbCdEfGhJ');assert.equal(first.heading,'视频标题');
 const next=await run('click',{ref:first.links[0].ref,page_key:first.page_key});assert.equal(next.navigate_to,anchor.href);
 await run('snapshot',{});await assert.rejects(run('click',{ref:'link-0',page_key:first.page_key}),/失效/);
 const fresh=(await run('snapshot',{})).page;anchor.href='https://evil.invalid';await assert.rejects(run('click',{ref:'link-0',page_key:fresh.page_key}),/失效/);
});
test('scroll and player return observed state and no arbitrary actions',async()=>{
 const {ctx,run}=fixture();assert.equal((await run('scroll',{y:500})).after_y,500);
 assert.equal((await run('player',{operation:'play'})).page.player.paused,false);
 assert.equal((await run('player',{operation:'pause'})).page.player.paused,true);
 assert.equal((await run('player',{operation:'seek',seconds:30})).page.player.current_time,30);
 await assert.rejects(run('execute',{code:'danger'}),/未开放/);
 ctx.location.href='https://message.bilibili.com/';await assert.rejects(run('snapshot',{}),/范围/);
});
