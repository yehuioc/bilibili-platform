"""单条视频的可回查材料：字幕、带时间画面；不把抽帧称为完整视听理解。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .media_stream import media_url, open_media
from .plugins import Action, Plugin
from .protocol import PlatformError, fingerprint
from .runtime import object_schema

TEMP_LIMIT=32*1024*1024
EVIDENCE_LIMIT=128*1024*1024


def ffmpeg(settings_file=None):
    configured=os.environ.get('BILI_FFMPEG')
    settings = Path(settings_file) if settings_file else Path('private/library/settings.json')
    if not configured and settings.is_file():
        configured=json.loads(settings.read_text(encoding='utf-8')).get('ffmpeg_file')
    if configured:
        path=Path(configured)
        if path.is_file():
            return str(path.resolve())
        raise PlatformError('configuration','BILI_FFMPEG 指定文件不存在')
    found=shutil.which('ffmpeg')
    if found:
        return found
    raise PlatformError('configuration','未找到FFmpeg；可用BILI_FFMPEG指定本机工具，不自动安装')


def extract(binary,media,target,seconds,check):
    check()
    command=[binary,'-hide_banner','-loglevel','error','-nostdin','-y','-rw_timeout','15000000',
             '-protocol_whitelist','http,tcp','-ss',str(seconds),
             '-i',str(media),'-frames:v','1','-vf',"scale='min(1280,iw)':-2",'-q:v','3',str(target)]
    process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    deadline=time.monotonic()+45
    try:
        while process.poll() is None:
            check()
            if time.monotonic()>deadline:
                raise PlatformError('transient','抽帧超时，未计为成功')
            time.sleep(.1)
        if process.returncode or not target.is_file() or target.stat().st_size<100:
            raise PlatformError('transient','无法从视频解码指定画面')
    except BaseException:
        process.kill();process.wait();target.unlink(missing_ok=True)
        raise
    finally:
        if process.stderr:
            process.stderr.close()


def choose_times(duration, requested):
    if not math.isfinite(duration) or duration<=0:
        raise PlatformError('identity','分P时长无效')
    values=requested or [duration*p for p in (.02,.2,.4,.6,.8,.96)]
    if any(type(t) not in (int,float) or not math.isfinite(t) or not 0<=t<duration for t in values):
        raise ValueError('画面时间必须位于当前分P时长内')
    return sorted({round(t,3) for t in values})


def packet_path(library,packet_id):
    if not isinstance(packet_id,str) or not re.fullmatch('[a-f0-9]{24}',packet_id):
        raise ValueError('视频材料ID无效')
    return library.root/'readings'/packet_id


def cleanup_temporary(library):
    """服务中断后只清理本模块的原视频临时文件，保留帧、字幕和评价。"""
    root=library.root/'readings'
    if not root.is_dir():
        return
    for folder in root.iterdir():
        if folder.is_dir() and re.fullmatch('[a-f0-9]{24}',folder.name) and folder.resolve().is_relative_to(root.resolve()):
            for name in ('source.mp4','source.partial'):
                (folder/name).unlink(missing_ok=True)


def load_packet(library,packet_id):
    path=packet_path(library,packet_id)/'packet.json'
    if not path.is_file():
        raise ValueError('视频材料不存在，请先读取')
    return json.loads(path.read_text(encoding='utf-8'))


def saved_reading(root,packet):
    path=root/'reading.json'
    if path.is_file():
        value=json.loads(path.read_text(encoding='utf-8'))
        if value.get('packet_hash')==packet.get('packet_hash') and packet.get('packet_hash'):
            return value
    return None


def read(ctx,args):
    binary=ffmpeg(ctx.runtime.library.root/'settings.json')
    client=ctx.platform()
    try:
        detail=client.video(args['bvid'])
        parts=detail.get('pages',[])
        number=args.get('part',1)
        if number>len(parts):
            raise ValueError('指定分P不存在')
        part=parts[number-1];cid=int(part['cid']);duration=float(part['duration'])
        times=choose_times(duration,args.get('times'))
        packet_id=fingerprint({'version':1,'account':client.identity['mid'],'bvid':detail['bvid'],'cid':cid,'duration':duration,'times':times})[:24]
        root=packet_path(ctx.runtime.library,packet_id);root.mkdir(parents=True,exist_ok=True)
        old=root/'packet.json'
        if old.is_file() and not args.get('refresh',False):
            cached=json.loads(old.read_text(encoding='utf-8'))
            if cached.get('subtitle_validation_version') == 2 and cached.get('media_transport',{}).get('source_saved') is False and cached.get('packet_hash') and time.time()-cached['created_at']<86400 and all(
                (root/r['file']).is_file() and hashlib.sha256((root/r['file']).read_bytes()).hexdigest()==r['sha256'] for r in cached['frames']):
                return {**cached,'cached':True,'manifest':str(old),'reading':saved_reading(root,cached)}
        ctx.checkpoint({'phase':'subtitles','bvid':detail['bvid'],'cid':cid})
        subtitle_error=None
        try:
            transcript=client.transcript(detail['bvid'],cid,int(detail['aid']),duration=duration)
        except PlatformError as exc:
            if exc.kind!='identity':
                raise
            # 拒用身份不符的字幕，不把它当作视频本身不可读；画面仍绑定已核对CID。
            transcript=None;subtitle_error=str(exc)
        segments=(transcript or {}).get('segments',[])
        selected=[];characters=0
        for row in segments:
            if characters+len(row['text'])>24000:
                break
            selected.append(row);characters+=len(row['text'])
        stream=client.playback(detail['bvid'],cid)
        stream_seconds=(stream.get('duration_ms') or 0)/1000
        if abs(stream_seconds-duration)>max(3,duration*.03):
            raise PlatformError('identity','播放材料时长与所选分P不一致，可能是试看，停止读取')
        evidence_root=ctx.runtime.library.root/'readings'
        retained=sum(p.stat().st_size for p in evidence_root.rglob('*') if p.is_file() and p.suffix not in ('.mp4','.partial'))
        if retained>=EVIDENCE_LIMIT or shutil.disk_usage(root).free<64*1024*1024:
            raise PlatformError('budget','证据已达128MiB或磁盘余量不足，已停止；不会自动删除阅读笔记')
        candidates=stream.get('urls') or [stream['url']]
        url=None
        for candidate in candidates:
            try:url=media_url(candidate);break
            except PlatformError:continue
        if not url:
            raise PlatformError('identity','平台未提供允许范围内的CDN地址，停止读取')
        frames=[]
        with open_media(url,check=ctx.check,max_bytes=TEMP_LIMIT) as transport:
            for index,seconds in enumerate(times):
                ctx.checkpoint({'phase':'frames','completed':index,'total':len(times),'bvid':detail['bvid']})
                filename=f'frame-{index:02d}.jpg';target=root/filename
                if target.is_file():
                    retained-=target.stat().st_size
                try:
                    extract(binary,transport.url,target,seconds,ctx.check)
                except PlatformError:
                    if transport.error:
                        raise transport.error
                    raise
                if transport.error:
                    raise transport.error
                retained+=target.stat().st_size
                if retained>EVIDENCE_LIMIT:
                    target.unlink(missing_ok=True)
                    raise PlatformError('budget','保留证据超过128MiB，已停止；阅读笔记未删除')
                frames.append({'id':f'f{index}','seconds':seconds,'file':filename,'path':str(target),
                               'url':f'/api/reading-file?packet={packet_id}&name={filename}',
                               'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
            transfer_bytes=transport.bytes_read
        coverage={'part':number,'parts_total':len(parts),'duration_seconds':duration,
                  'text':'subtitle_identity_rejected' if subtitle_error else 'available_subtitle_complete' if selected and len(selected)==len(segments) else 'subtitle_truncated' if selected else 'no_subtitle',
                  'subtitle_error':subtitle_error,
                  'subtitle_source':(transcript or {}).get('source'),'visual':'sampled','frame_times':times,
                  'audio':'not_independently_analyzed','whole_video_reviewed':False}
        result={'api_version':1,'subtitle_validation_version':2,'packet_id':packet_id,'bvid':detail['bvid'],'aid':detail['aid'],'cid':cid,
                'title':detail['title'],'created_at':time.time(),'coverage':coverage,'segments':selected,'frames':frames,
                'inputs':[{'type':'text','field':'segments'}]+[{'type':'image','frame_id':r['id'],'path':r['path']} for r in frames],
                'limits':['画面为抽样；可按指定时间继续取帧','字幕不能覆盖语气、音乐和全部画面','未读取的分P不包含在当前材料中'],
                'cached':False,'manifest':str(old),'media_transport':{'mode':'bounded_range_stream','bytes_read':transfer_bytes,'source_saved':False}}
        result['packet_hash']=fingerprint({'bvid':result['bvid'],'cid':cid,'coverage':coverage,
                                           'segments':selected,'frames':[{'id':f['id'],'sha256':f['sha256'],'seconds':f['seconds']} for f in frames]})
        temporary=root/'packet.partial'
        temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(old)
        return {**result,'reading':saved_reading(root,result)}
    finally:
        client.close()


def text(ctx,args):
    """空间受限时仍可单独取得字幕；明确没有读取画面与原音。"""
    client=ctx.platform()
    try:
        detail=client.video(args['bvid']);number=args.get('part',1)
        if number>len(detail.get('pages',[])):
            raise ValueError('指定分P不存在')
        cid=int(detail['pages'][number-1]['cid'])
        result=client.transcript(detail['bvid'],cid,int(detail['aid']),duration=float(detail['pages'][number-1]['duration']))
        segments=(result or {}).get('segments',[])
        return {'bvid':detail['bvid'],'cid':cid,'title':detail['title'],'part':number,'parts_total':len(detail['pages']),
                'segments':segments,'identity_verified':bool(result and result.get('identity_verified')),
                'coverage':{'text':'available_subtitle_complete' if segments else 'no_subtitle',
                            'visual':'not_read','audio':'not_independently_analyzed','whole_video_reviewed':False}}
    finally:
        client.close()


def record(ctx,args):
    packet=load_packet(ctx.runtime.library,args['packet_id'])
    ids={r['id']:r for r in packet['frames']}
    for row in args['observations']:
        if row['frame_id'] not in ids:
            raise ValueError('画面引用不属于本次视频材料')
    value={'packet_id':packet['packet_id'],'packet_hash':packet['packet_hash'],'bvid':packet['bvid'],'summary':args['summary'],
           'observations':[{**r,'seconds':ids[r['frame_id']]['seconds']} for r in args['observations']],
           'limitations':args['limitations'],'coverage':packet['coverage'],'reader':args['reader'],
           'created_at':time.time()}
    path=packet_path(ctx.runtime.library,args['packet_id'])/'reading.json'
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return value


def plugin():
    string={'type':'string','minLength':1,'maxLength':4000}
    return Plugin('video',1,('read','text','record'),(
        Action('video.text','只取指定分P字幕，不下载视频或读取画面',object_schema({
            'bvid':{'type':'string','pattern':r'BV[A-Za-z0-9]{10}'},
            'part':{'type':'integer','minimum':1,'maximum':1000}},('bvid',)),text),
        Action('video.read','读取单条视频的已核对字幕与带时间抽样画面',object_schema({
            'bvid':{'type':'string','pattern':r'BV[A-Za-z0-9]{10}'},
            'part':{'type':'integer','minimum':1,'maximum':1000},
            'times':{'type':'array','minItems':1,'maxItems':12,'items':{'type':'number','minimum':0,'maximum':86400}},
            'refresh':{'type':'boolean'}},('bvid',)),read),
        Action('video.record','保存会话或其他阅读器对已取得材料的阅读结果，不修改收藏',object_schema({
            'packet_id':{'type':'string','pattern':'[a-f0-9]{24}'},'reader':string,'summary':string,
            'observations':{'type':'array','maxItems':24,'items':object_schema({'frame_id':string,'text':string},('frame_id','text'))},
            'limitations':{'type':'array','maxItems':20,'items':string}},('packet_id','reader','summary','observations','limitations')),record,'write'),
    ))
