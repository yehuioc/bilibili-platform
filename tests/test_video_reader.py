import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from bili_platform.client import BilibiliClient
from bili_platform.library import Library
from bili_platform.protocol import PlatformError
from bili_platform.video_reader import (
    choose_times,
    cleanup_temporary,
    ffmpeg,
    media_url,
    packet_path,
    read,
    record,
)


def test_ffmpeg_uses_explicit_local_configuration_without_machine_paths(monkeypatch,tmp_path):
    monkeypatch.delenv('BILI_FFMPEG',raising=False)
    monkeypatch.setattr('bili_platform.video_reader.shutil.which',lambda name:None)
    config=tmp_path/'settings.json'
    with pytest.raises(PlatformError,match='未找到'):
        ffmpeg(config)
    local=tmp_path/'configured-ffmpeg.exe';local.write_bytes(b'synthetic')
    config.write_text(json.dumps({'ffmpeg_file':str(local)}),encoding='utf-8')
    assert ffmpeg(config)==str(local.resolve())
    override=tmp_path/'override-ffmpeg.exe';override.write_bytes(b'synthetic')
    monkeypatch.setenv('BILI_FFMPEG',str(override))
    assert ffmpeg(config)==str(override.resolve())
    monkeypatch.setenv('BILI_FFMPEG',str(tmp_path/'missing.exe'))
    with pytest.raises(PlatformError,match='不存在'):
        ffmpeg(config)


def test_compact_members_are_complete_typed_and_identity_bound():
    payload=[{'id':1,'type':2,'bv_id':'BV1AbCdEfGhJ'}]
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':0,'data':payload}))) as http:
        client=BilibiliClient(http=http,pace=lambda:None)
        assert client.members({'id':7,'media_count':1})[0]['bvid']=='BV1AbCdEfGhJ'
        with pytest.raises(PlatformError,match='数量'):
            client.members({'id':7,'media_count':2})
        payload.append(payload[0])
        with pytest.raises(PlatformError,match='重复'):
            client.members({'id':7})
        client.close()


def test_times_media_and_artifact_paths_are_bounded(tmp_path):
    assert choose_times(10,[9,1,1])==[1,9]
    for times in ([10],[-1],[float('nan')]):
        with pytest.raises(ValueError):choose_times(10,times)
    for url in ('http://a.bilivideo.com/x','https://bilivideo.com.evil.test/x','https://127.0.0.1/x','https://u:p@a.bilivideo.com/x'):
        with pytest.raises(PlatformError):media_url(url)
    assert media_url('https://a.bilivideo.com/x')
    with pytest.raises(ValueError):packet_path(Library(tmp_path),'../settings')


def test_restart_cleanup_only_removes_owned_temporary_video(tmp_path):
    lib=Library(tmp_path);root=packet_path(lib,'a'*24);root.mkdir(parents=True)
    for name in ('source.mp4','source.partial','frame-00.jpg','reading.json','original.mp4'):
        (root/name).write_bytes(b'kept')
    cleanup_temporary(lib)
    assert not (root/'source.mp4').exists() and not (root/'source.partial').exists()
    assert (root/'frame-00.jpg').exists() and (root/'reading.json').exists() and (root/'original.mp4').exists()


def test_video_packet_cache_and_record_do_not_claim_whole_video(monkeypatch,tmp_path):
    library=Library(tmp_path);calls=[]
    class Client:
        def __init__(self):self.identity={'mid':42}
        def video(self,bvid):return {'bvid':bvid,'aid':1,'title':'测试','pages':[{'cid':2,'duration':10},{'cid':3,'duration':20}]}
        def transcript(self,*args,**kwargs):calls.append('subtitle');return {'source':'subtitle','segments':[{'start':0,'end':2,'text':'字幕'}]}
        def playback(self,*args):return {'url':'https://x.bilivideo.com/a','size':200,'duration_ms':10000}
        def close(self):pass
    ctx=SimpleNamespace(runtime=SimpleNamespace(library=library),platform=Client,check=lambda:None,checkpoint=lambda v:None)
    monkeypatch.setattr('bili_platform.video_reader.ffmpeg',lambda *args:'ffmpeg')
    @contextmanager
    def fake_media(*args,**kwargs):
        yield SimpleNamespace(url='http://127.0.0.1:1/token',bytes_read=1234,error=None)
    monkeypatch.setattr('bili_platform.video_reader.open_media',fake_media)
    monkeypatch.setattr('bili_platform.video_reader.extract',lambda b,m,p,t,c:p.write_bytes(b'jpeg'+str(t).encode()))
    args={'bvid':'BV1AbCdEfGhJ','times':[1,9]}
    packet=read(ctx,args)
    assert not list(tmp_path.rglob('*.mp4')) and not list(tmp_path.rglob('*.partial'))
    assert packet['coverage']['visual']=='sampled' and not packet['coverage']['whole_video_reviewed']
    assert packet['coverage']['parts_total']==2 and packet['coverage']['audio']=='not_independently_analyzed'
    assert len(packet['frames'])==2
    assert read(ctx,args)['cached'] and calls==['subtitle']
    manifest=packet_path(library,packet['packet_id'])/'packet.json'
    legacy=json.loads(manifest.read_text('utf-8'))
    legacy.pop('subtitle_validation_version')
    manifest.write_text(json.dumps(legacy),encoding='utf-8')
    assert not read(ctx,args)['cached'] and len(calls)==2
    (packet_path(library,packet['packet_id'])/'frame-00.jpg').write_bytes(b'changed')
    assert not read(ctx,args)['cached'] and len(calls)==3
    result=record(ctx,{'packet_id':packet['packet_id'],'reader':'current-session','summary':'画面与字幕','observations':[{'frame_id':'f0','text':'观察'}],'limitations':['抽样']})
    assert result['observations'][0]['seconds']==1
    assert read(ctx,args)['reading']['summary']=='画面与字幕'
    with pytest.raises(ValueError,match='引用'):
        record(ctx,{'packet_id':packet['packet_id'],'reader':'session','summary':'x','observations':[{'frame_id':'f99','text':'假引用'}],'limitations':[]})
    assert json.loads((packet_path(library,packet['packet_id'])/'reading.json').read_text('utf-8'))['bvid']==args['bvid']
    def mismatched(*args,**kwargs):raise PlatformError('identity','错误字幕')
    monkeypatch.setattr(Client,'transcript',mismatched)
    changed=read(ctx,{**args,'refresh':True})
    assert changed['coverage']['text']=='subtitle_identity_rejected' and changed['frames']
    assert changed['reading'] is None and changed['segments']==[]
    def failed(*args):raise PlatformError('cancelled','停止')
    monkeypatch.setattr('bili_platform.video_reader.extract',failed)
    with pytest.raises(PlatformError):read(ctx,{**args,'refresh':True})
    assert not list(tmp_path.rglob('*.mp4')) and not list(tmp_path.rglob('*.partial'))
