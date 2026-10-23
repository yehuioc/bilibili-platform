import {readFile,writeFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const [upstream,datasetPath,receiptPath]=process.argv.slice(2);
const {runBackgroundScenario}=await import(pathToFileURL(path.resolve(upstream,'extension/tests/helpers/background-runtime-harness.mjs')));
const dataset=JSON.parse(await readFile(datasetPath,'utf8'));
const input={videos:dataset.videos.map(v=>({bvid:v.bvid,title:v.title,description:v.description||'',aid:v.aid}))};
const out=runBackgroundScenario({
  exports:['saveVideoSelectionToState','syncFromBilibiliToState','buildJsonExportResult'],input,
  setupSource:`
    globalThis.fetch=async request=>{
      const url=String(request);
      const data=url.includes('/x/web-interface/nav')?{isLogin:true,mid:42}:
        url.includes('/x/v3/fav/folder/created/list-all')?{list:[{id:99,title:'合成20条',media_count:20}]}:
        url.includes('/x/v3/fav/resource/list')?{has_more:false,info:{media_count:20},medias:input.videos.map(v=>({bvid:v.bvid,id:v.aid,title:v.title,intro:'远端最新简介',upper:{name:'固定样本',mid:42},fav_time:1700000000}))}:null;
      if(data===null)throw Error('Unexpected network request: '+url);
      return new Response(JSON.stringify({code:0,data}),{status:200});
    };
  `,
  scenarioSource:`
    const state={counters:{folder:1,video:1,folderItem:1,tag:1,videoTag:1},folders:[],videos:[],folderItems:[],tags:[],videoTags:[],followedUps:[],syncMeta:{},ai:{}};
    state.folders.push({id:1,name:'合成20条',description:null,remoteMediaId:99,sortOrder:1,deletedAt:null,createdAt:1,updatedAt:1});state.counters.folder=2;
    for(const v of input.videos)saveVideoSelectionToState(state,{...v,coverUrl:'',uploader:'固定样本',description:'私人备注不可写在这里',folderIds:[1],customTags:v.title.toLowerCase().includes('example-skill')?['ExampleSkill']:[],systemTags:[]});
    const before=JSON.stringify(state.videoTags);
    const result=await syncFromBilibiliToState(state,{});
    const exported=JSON.parse(buildJsonExportResult(state).content);
    return {count:state.videos.length,manualTagsPreserved:before===JSON.stringify(state.videoTags),customTagLinks:state.videoTags.length,descriptionRefreshed:state.videos.every(v=>v.description==='远端最新简介'),summary:result.summary,exportedTagLinks:exported.videoTags.length};
  `
});
assert.equal(out.result.count,20);
assert.equal(out.result.manualTagsPreserved,true);
assert.equal(out.result.customTagLinks,3);
assert.equal(out.result.exportedTagLinks,3);
assert.equal(out.result.descriptionRefreshed,true);
const receipt={source:'ac2c56e1bcb69b4c28cfeca35695a77a5322ed35',mode:'original core + mocked remote GET, synthetic fixed20 identities',...out.result};
await writeFile(receiptPath,JSON.stringify(receipt,null,2));
console.log(JSON.stringify(receipt));
