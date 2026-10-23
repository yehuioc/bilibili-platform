// 合成数据的离线源码试跑：真实浏览器 IndexedDB，Chrome 账号/网络接口不启用。
import { readFile, writeFile, mkdir, cp } from 'node:fs/promises';
import path from 'node:path';
import { stripTypeScriptTypes } from 'node:module';

const [upstream, output, dataset] = process.argv.slice(2);
if (!upstream || !output || !dataset) throw Error('usage: upstream output dataset');
await mkdir(output, { recursive: true });
await cp(path.join(upstream, 'LICENSE'), path.join(output, 'BiliShelf-LICENSE.txt'));
await cp(path.join(upstream, 'extension/shared'), path.join(output, 'shared'), { recursive: true });
let source = await readFile(path.join(upstream, 'extension/entrypoints/background.ts'), 'utf8');
source = source.replaceAll('../shared/', './shared/');
source = source.replace('const DB_NAME = "bilishelf-local-db";', 'const DB_NAME = "bilishelf-fixed20-trial";');
for (const name of ['defaultState', 'readState', 'writeState', 'saveVideoSelectionToState', 'handleReadOnlyApi']) {
  source = source.replace(new RegExp('(?<!export )(async )?function '+name+'\\('), (m) => 'export '+m);
}
await writeFile(path.join(output, 'background.js'), stripTypeScriptTypes(source, { mode: 'transform' }));
await cp(dataset, path.join(output, 'fixture.json'));
await writeFile(path.join(output, 'index.html'), '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>BiliShelf 合成20条机制试跑</title><h1>BiliShelf 合成20条机制试跑</h1><p>仅本地数据；真实浏览器 IndexedDB；不连接B站或模型。</p><pre id="result">运行中</pre><script type="module" src="/trial/run.js"></script></html>');
await writeFile(path.join(output, 'run.js'), `
window.defineBackground = fn => fn;
window.chrome = {alarms:{clear(){},create(){}},runtime:{onMessage:{addListener(){}}}};
const core = await import('./background.js');
const fixture = await (await fetch('./fixture.json')).json();
const result = [];
const check = (name, condition) => {result.push({name,pass:!!condition});if(!condition)throw Error(name);};
try {
  let state = await core.readState();
  const fresh = !state.videos.length;
  if(fresh){
    state.folders.push({id:1,name:'合成20条',description:null,remoteMediaId:null,sortOrder:1,deletedAt:null,createdAt:1,updatedAt:1});
    state.counters.folder=2;
    for(const video of fixture.videos){
      const novel = video.title.toLowerCase().includes('example-skill');
      const saved = core.saveVideoSelectionToState(state,{bvid:video.bvid,title:video.title,coverUrl:'',uploader:'固定样本',description:video.description||'',folderIds:[1],customTags:novel?['ExampleSkill']:[],systemTags:[]});
      check('保存 '+video.bvid,saved.ok);
    }
    await core.writeState(state);
  }
  check('合成20条',state.videos.length===20);
  let params=new URLSearchParams({customTag:'ExampleSkill',page:'1',pageSize:'100'});
  let query=core.handleReadOnlyApi(state,'/videos',params);
  check('ExampleSkill标签命中3条',query.data.items.length===3);
  const originalTitle=state.videos.find(v=>v.bvid===query.data.items[0].bvid).title;
  state.videos.find(v=>v.bvid===query.data.items[0].bvid).title='标题中没有工具名称';
  query=core.handleReadOnlyApi(state,'/videos',params);
  check('标题不含关键词仍命中',query.data.items.length===3);
  state.videos.find(v=>v.bvid===query.data.items[0].bvid).title=originalTitle;
  const backup=core.buildJsonExportResult(state);
  const parsed=core.parseImportRows('json',backup.content);
  check('JSON备份往返视频完整',parsed.rows.length===20);
  check('JSON备份往返标签完整',parsed.rows.filter(v=>v.customTags?.includes('ExampleSkill')).length===3);
  check(fresh?'真实IndexedDB写入完成':'页面重启后读取保留',state.videoTags.length===3);
  document.getElementById('result').textContent=JSON.stringify({phase:fresh?'first-write':'reloaded',pass:true,checks:result},null,2);
}catch(e){document.getElementById('result').textContent=JSON.stringify({pass:false,error:e.message,checks:result},null,2);}
`);
console.log(JSON.stringify({output, source: 'ac2c56e1bcb69b4c28cfeca35695a77a5322ed35'}));
