"""本机有界Range转发：FFmpeg按需读CDN，不保存原视频、不转发Cookie。"""
import re
import secrets
import threading
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from .protocol import HEADERS, PlatformError


def media_url(raw):
    if not isinstance(raw,str):
        raise PlatformError('identity','视频流地址无效')
    u=urlsplit(raw)
    if (u.scheme!='https' or u.username or u.password or u.port not in (None,443)
            or not any((u.hostname or '').endswith('.'+d) for d in ('bilivideo.com','bilivideo.cn'))):
        raise PlatformError('identity','视频材料地址不在允许的B站CDN范围')
    return raw


@contextmanager
def open_media(url,*,check=lambda:None,max_bytes=32*1024*1024):
    media_url(url)
    state=SimpleNamespace(bytes_read=0,requests=0,error=None,url=None)
    lock=threading.Lock();token='/'+secrets.token_urlsafe(24)
    http=httpx.Client(headers={**HEADERS,'Accept-Encoding':'identity'},timeout=20,follow_redirects=False)

    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'

        def log_message(self,*args):
            pass

        def do_HEAD(self):
            self.transfer('HEAD')

        def do_GET(self):
            self.transfer('GET')

        def transfer(self,method):
            self.close_connection=True
            if self.path!=token or self.headers.get('Host')!=f'127.0.0.1:{server.server_port}':
                self.send_error(403);return
            span=self.headers.get('Range','bytes=0-')
            if not re.fullmatch(r'bytes=\d+-\d*',span):
                self.send_error(400);return
            sent=False
            try:
                check()
                with lock:
                    if state.error:
                        raise state.error
                    state.requests+=1
                # FFmpeg传来的Host/Authorization/Cookie等均不向CDN转发。
                request=http.build_request(method,url,headers={'Range':span})
                request.headers.pop('Cookie',None)
                with closing(http.send(request,stream=True)) as response:
                    if response.status_code not in (200,206):
                        raise PlatformError('transient',f'视频Range读取失败 HTTP {response.status_code}')
                    self.send_response(response.status_code)
                    for name in ('Content-Length','Content-Range','Content-Type','Accept-Ranges'):
                        if name in response.headers:
                            self.send_header(name,response.headers[name])
                    self.send_header('Connection','close');self.end_headers();sent=True
                    if method=='HEAD':
                        return
                    for block in response.iter_bytes(65536):
                        check()
                        with lock:
                            state.bytes_read+=len(block)
                            if state.bytes_read>max_bytes:
                                raise PlatformError('budget','本次按需视频读取达到32MiB传输上限，已停止；没有保存原视频')
                        self.wfile.write(block)
            except ConnectionError:
                pass  # FFmpeg跳转时主动关闭旧Range，属于正常行为。
            except (PlatformError,httpx.HTTPError) as exc:
                state.error=exc if isinstance(exc,PlatformError) else PlatformError('transient','视频Range传输失败')
                if not sent:
                    self.send_error(502)

    class Server(ThreadingHTTPServer):
        daemon_threads=True

    server=Server(('127.0.0.1',0),Handler)
    state.url=f'http://127.0.0.1:{server.server_port}{token}'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield state
    finally:
        server.shutdown();server.server_close();http.close();thread.join(timeout=2)
