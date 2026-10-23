import test from 'node:test';
import assert from 'node:assert/strict';
import {serviceUrl, currentPage} from '../extension/protocol.mjs';

test('only loopback service origins, no embedded credentials or arbitrary URLs',()=>{
  assert.equal(serviceUrl('http://127.0.0.1:6861/'),'http://127.0.0.1:6861');
  for(const v of ['https://evil.invalid','http://localhost:6861','http://127.0.0.1.evil/','http://user:password@127.0.0.1:6861','http://127.0.0.1:6861/path']) assert.throws(()=>serviceUrl(v));
});
test('current page preserves case-sensitive BVID and removes tracking parameters',()=>{
  const p=currentPage({url:'https://www.bilibili.com/video/BV1AbCdEfGhJ/?spm_id_from=test',title:'真实标题'});
  assert.deepEqual(p,{url:'https://www.bilibili.com/video/BV1AbCdEfGhJ/',title:'真实标题',bvid:'BV1AbCdEfGhJ'});
  assert.equal(currentPage({url:'https://space.bilibili.com/42/favlist?fid=17&from=private',title:'收藏'}).url,'https://space.bilibili.com/42/favlist?fid=17');
  for(const url of ['https://www.bilibili.com.evil/video/BV1AbCdEfGhJ','https://space.bilibili.com/video/BV1AbCdEfGhJ','https://www.bilibili.com/','https://chatgpt.com/']) assert.throws(()=>currentPage({url}));
  assert.throws(()=>currentPage({}));
});
