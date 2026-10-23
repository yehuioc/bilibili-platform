import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

test('idle panel waits at server; a user action aborts the wait instead of queuing behind it',async()=>{
 const code=(await fs.readFile(new URL('../extension/panel.js',import.meta.url),'utf8')).replace(/^import[^\n]+\n/,'');
 const elements=new Map(),requests=[],timers=[];let aborted=false;
 const element=id=>{if(!elements.has(id))elements.set(id,{value:'',textContent:'',dataset:{},checked:false});return elements.get(id);};
 const binding={tab_id:7,session:'a'.repeat(36),url:'https://www.bilibili.com/video/BV1AbCdEfGhJ/',control:true};
 const context={console,URL,AbortController,AbortSignal,crypto:webcrypto,Date,Error,JSON,Promise,
  setTimeout:(fn,ms)=>{timers.push(ms);return 1;},document:{getElementById:element},serviceUrl:x=>x,
  chrome:{storage:{local:{get:async()=>({connection:{base:'http://127.0.0.1:6861',token:'secret'}}),set:async()=>{}},session:{get:async()=>({binding}),set:async()=>{}}},
   tabs:{get:async()=>({id:7,url:binding.url}),create:async()=>{}},scripting:{executeScript:async o=>o.files?[{}]:[{result:{page:{url:binding.url,title:'actual'}}}]}},
  fetch:async(url,options)=>{const b=JSON.parse(options.body);requests.push(b);if(b.wait){return await new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{aborted=true;reject(new DOMException('cancel','AbortError'));},{once:true}));}return {ok:true,json:async()=>({messages:[],command:null,stopped:false})};}
 };
 vm.runInNewContext(code,context);
 for(let i=0;i<20&&!requests.some(r=>r.wait===15);i++)await new Promise(resolve=>setImmediate(resolve));
 assert.ok(requests.some(r=>r.wait===15));
 await element('snapshot').onclick();
 assert.ok(aborted);
 assert.match(element('debug-result').textContent,/actual/);
 assert.equal(requests.filter(r=>r.wait===15).length,1);
 assert.ok(!timers.includes(1000));
});
