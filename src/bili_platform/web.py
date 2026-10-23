"""仅回环地址的私人收藏页面，平台调用全部复用公共 SDK。"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .client import BilibiliClient
from .extension_bridge import ORIGIN, ExtensionBridge
from .library import Library, dumps
from .protocol import PlatformError
from .runtime import Runtime

ASSETS = Path(__file__).parent / "web_assets"


class Sync:
    def __init__(self, library, cookie_file):
        self.library, self.cookie_file = library, cookie_file
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        old = library.job()
        if old and old.get("status") == "running":
            library.job({**old, "status": "interrupted", "error": "服务已重启，可重新同步；已有标注保留"}, old["id"])

    def client(self):
        if not self.cookie_file or not Path(self.cookie_file).is_file():
            raise PlatformError("auth", "未配置B站会话；本地标注可用，同步暂不可用")
        client = BilibiliClient.from_cookie_file(self.cookie_file)
        try:
            if client.nav()["mid"] != self.library.account() and self.library.account() is not None:
                raise PlatformError("identity", "登录账号与本地库不同，已停止读取")
        except Exception:
            client.close()
            raise
        return client

    def folders(self):
        client = self.client()
        try:
            return [{"id": r["id"], "title": r["title"], "count": r.get("media_count")}
                    for r in client.folders()]
        finally:
            client.close()

    def start(self, body):
        kind = body.get("kind", "metadata")
        if kind not in ("metadata", "import", "transcript"):
            raise ValueError("同步类型无效")
        if not self.lock.acquire(blocking=False):
            raise PlatformError("conflict", "已有同步任务，请等待完成或停止")
        if not self.cookie_file or not Path(self.cookie_file).is_file():
            self.lock.release()
            raise PlatformError("auth", "未配置有效会话；本地标注不受影响")
        self.cancelled.clear()
        job = {"kind": kind, "status": "running", "done": 0, "total": 0,
               "errors": [], "started": time.time(), "message": "核对账号与读取范围"}
        job_id = self.library.job(job)
        threading.Thread(target=self.run, args=(body, job, job_id), daemon=True).start()
        return {**job, "id": job_id}

    def run(self, body, job, job_id):
        client = None
        try:
            client = self.client()
            state = self.library.state()
            if job["kind"] == "import":
                folder_id = int(body.get("folder_id", 0))
                limit = int(body.get("limit", 20))
                if limit not in (0, 20, 50, 100):
                    raise ValueError("导入范围无效")
                folder = next((f for f in client.folders() if f["id"] == folder_id), None)
                if not folder:
                    raise ValueError("收藏夹不存在，请重新读取")
                rows, complete = client.inventory(folder, limit=limit)
                source = {"id": folder_id, "title": folder["title"], "complete": complete,
                          "scope": "all" if complete else "recent", "count": len(rows), "updated": time.time()}
                videos = [{"bvid": r.get("bvid"), "aid": r.get("id") or r.get("aid"),
                           "title": r.get("title", ""), "author": r.get("upper", {}).get("name", ""),
                           "cover": r.get("cover", ""), "description": r.get("intro", ""),
                           "availability": "listed", "folders": [folder_id]} for r in rows if r.get("bvid")]
                self.library.update_metadata(client.identity["mid"], videos, source)
                state["account"] = client.identity["mid"]
            else:
                chosen = body.get("bvids")
                if chosen is not None and (not isinstance(chosen, list) or not chosen):
                    raise ValueError("请选择要更新的视频")
                videos = [v for v in state["videos"] if chosen is None or v["bvid"] in chosen]
                if chosen and {v["bvid"] for v in videos} != set(chosen):
                    raise ValueError("选中的视频不属于当前本地库")
                if job["kind"] == "transcript" and len(videos) != 1:
                    raise ValueError("字幕读取每次限一个视频")
            job["total"] = len(videos)
            for item in videos:
                if self.cancelled.is_set():
                    job["status"] = "cancelled"
                    break
                bvid = item["bvid"]
                job["message"] = item.get("title", bvid)
                self.library.job(job, job_id)
                try:
                    detail = client.video(bvid)
                    if item.get("aid") and int(detail["aid"]) != int(item["aid"]):
                        raise PlatformError("identity", "视频身份不符")
                    value = {"bvid": bvid, "aid": detail["aid"], "title": detail["title"],
                             "description": detail.get("desc", ""), "cover": detail.get("pic", ""),
                             "author": detail.get("owner", {}).get("name", ""),
                             "duration": detail.get("duration"), "published": detail.get("pubdate"),
                             "synced": time.time(), "availability": "available"}
                    if job["kind"] == "transcript":
                        parts = []
                        pages = detail.get("pages", [])
                        if len(pages) > 20:
                            raise ValueError("分P超过20，本轮不自动批量读取")
                        for page in pages:
                            part = client.transcript(bvid, page["cid"], detail["aid"])
                            if part:
                                part.pop("raw", None)
                                parts.append(part)
                        value.update(parts=parts, expected_parts=len(pages),
                                     all_parts_verified=bool(pages) and len(parts) == len(pages))
                    self.library.update_metadata(state["account"], [value])
                except PlatformError as exc:
                    job["errors"].append({"bvid": bvid, "kind": exc.kind, "message": str(exc)})
                    if exc.kind in ("auth", "rate_limit", "identity"):
                        raise
                job["done"] += 1
                self.library.job(job, job_id)
            if job["status"] == "running":
                job["status"] = "partial" if job["errors"] else "complete"
        except Exception as exc:  # noqa: BLE001 -- 任务边界须持久记录失败，并释放同步锁
            job["status"] = "failed"
            job["error"] = str(exc) if isinstance(exc, (PlatformError, ValueError)) else "同步失败；请检查会话与网络后重试"
        finally:
            if client:
                client.close()
            job["finished"] = time.time()
            self.library.job(job, job_id)
            self.lock.release()


def make_server(library, *, port=8766, cookie_file=None, trial_dir=None):
    secret = secrets.token_urlsafe(32)
    sync = Sync(library, cookie_file)
    bridge = ExtensionBridge(library)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, content, mime="application/json; charset=utf-8", *, cookie=False, download=False):
            # Windows在带未读请求体时关闭socket可能丢掉错误响应，先有界排空小请求。
            if status >= 400 and self.command == "POST" and not getattr(self,"body_consumed",False):
                try:
                    size=int(self.headers.get("Content-Length",0))
                    if 0 < size <= 16384:
                        self.connection.settimeout(2)
                        self.rfile.read(size)
                except (OSError,ValueError):
                    pass
                self.body_consumed=True
            raw = content.encode("utf-8") if isinstance(content, str) else content
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if getattr(self, "bridge_origin", None):
                self.send_header("Access-Control-Allow-Origin", self.bridge_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' https://*.hdslb.com data:; frame-src https://player.bilibili.com; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            if cookie:
                self.send_header("Set-Cookie", f"library_session={secret}; HttpOnly; SameSite=Strict; Path=/")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="bili-library-backup.json"')
            self.end_headers()
            self.wfile.write(raw)

        def authorize(self):
            if self.headers.get("Host") not in allowed:
                raise PermissionError("仅接受本机地址")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in allowed}:
                raise PermissionError("不接受其他网站请求")
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if not cookie.get("library_session") or not secrets.compare_digest(cookie["library_session"].value, secret):
                raise PermissionError("请先打开收藏库首页")
            if self.command != "GET" and self.headers.get("X-Library-Token") != secret:
                raise PermissionError("缺少本地操作凭据")

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def do_OPTIONS(self):
            try:
                self.check_bridge_origin()
                if urlsplit(self.path).path not in ("/bridge/pair", "/bridge/poll", "/bridge/message", "/bridge/result"):
                    raise PermissionError("未开放此连接操作")
                self.send(204, b"")
            except PermissionError as exc:
                self.send(403, dumps({"error": str(exc)}))

        def check_bridge_origin(self):
            origin = self.headers.get("Origin", "")
            if self.headers.get("Host") not in allowed or not ORIGIN.fullmatch(origin):
                raise PermissionError("仅接受扩展到本机的连接")
            self.bridge_origin = origin
            return origin

        def read_body(self, maximum=30_000_000):
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("只接受JSON请求")
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= maximum:
                raise ValueError("请求为空或超过允许大小")
            raw = self.rfile.read(length)
            self.body_consumed=True
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise TypeError("请求结构无效")
            return body

        def handle_request(self):
            try:
                path = urlsplit(self.path).path
                if path.startswith("/bridge/"):
                    origin = self.check_bridge_origin()
                    if self.command != "POST":
                        raise PermissionError("扩展连接仅接受POST")
                    body = self.read_body(131072 if path == "/bridge/result" else 8192)
                    if path == "/bridge/pair":
                        if set(body) != {"code"}:
                            raise ValueError("配对参数无效")
                        result = bridge.pair(origin, body["code"])
                    else:
                        bearer = self.headers.get("Authorization", "")
                        if not bearer.startswith("Bearer "):
                            raise PermissionError("配对已失效，请重新配对")
                        result = bridge.extension_request(origin, bearer[7:], path.removeprefix("/bridge/"), body)
                    return self.send(200, dumps(result))
                if path == "/control":
                    if (self.command != "POST" or self.headers.get("Host") not in allowed or self.headers.get("Origin")
                            or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + runtime.control_token)):
                        raise PermissionError("仅接受已授权本机工具调用")
                    body=self.read_body(16384)
                    if body.get("op") == "catalog" and set(body) == {"op"}:
                        result=runtime.catalog()
                    elif body.get("op") == "call" and set(body) == {"op","request"}:
                        result=runtime.submit(body["request"])
                    elif body.get("op") in ("job","cancel") and set(body) == {"op","job_id"}:
                        result=runtime.job(body["job_id"]) if body["op"] == "job" else runtime.cancel(body["job_id"])
                    else:
                        raise ValueError("工具请求无效")
                    return self.send(200,dumps(result))
                if path in ("/", "/connection") and self.command == "GET":
                    if self.headers.get("Host") not in allowed:
                        raise PermissionError("仅接受本机地址")
                    name = "index.html" if path == "/" else "connection.html"
                    return self.send(200, (ASSETS / name).read_bytes(), "text/html; charset=utf-8", cookie=True)
                self.authorize()
                if self.command == "GET":
                    if path == '/api/runtime/job':
                        query=parse_qs(urlsplit(self.path).query)
                        return self.send(200,dumps(runtime.job(query.get('id',[''])[0])))
                    if path == '/api/reading-file':
                        from .video_reader import packet_path
                        query=parse_qs(urlsplit(self.path).query)
                        root=packet_path(library,query.get('packet',[''])[0]).resolve()
                        name=query.get('name',[''])[0]
                        import re
                        if not re.fullmatch(r'frame-\d{2}\.jpg',name):
                            raise ValueError('只允许读取本次视频画面')
                        image=(root/name).resolve()
                        if not image.is_relative_to(root) or not image.is_file():
                            raise ValueError('视频画面不存在')
                        return self.send(200,image.read_bytes(),'image/jpeg')
                    if path == "/api/state":
                        return self.send(200, dumps({**library.state(), "token": secret,
                                                     "extension_bridge": True,
                                                     "sync_available": bool(cookie_file and Path(cookie_file).is_file())}))
                    if path == "/api/extension/state":
                        return self.send(200, dumps({**bridge.state(), "token": secret}))
                    if path == "/api/runtime/state":
                        return self.send(200,dumps({"catalog":runtime.catalog(),"jobs":runtime.jobs(),"bridge":bridge.state(),"token":secret,
                                                   "session_configured":bool(cookie_file and Path(cookie_file).is_file())}))
                    if path == "/api/backup":
                        return self.send(200, dumps(library.backup()), download=True)
                    if path == "/api/backups":
                        return self.send(200, dumps(library.local_backups()))
                    if path == "/api/job":
                        return self.send(200, dumps(sync.library.job()))
                    if path == "/api/platform/folders":
                        return self.send(200, dumps(sync.folders()))
                    assets = {"/app.js": "app.js", "/style.css": "style.css",
                              "/connection.js": "connection.js", "/connection.css": "connection.css",
                              "/vendor/custom-tag-suggestions.js": "vendor/custom-tag-suggestions.js"}
                    target = ASSETS / assets[path] if path in assets else None
                    if trial_dir and path.startswith("/trial/"):
                        base = Path(trial_dir).resolve()
                        target = (base / path[len("/trial/"):]).resolve()
                        if not target.is_relative_to(base):
                            raise PermissionError("路径无效")
                    if target and target.is_file():
                        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                        if target.suffix in (".js", ".mjs"):
                            mime = "text/javascript"
                        return self.send(200, target.read_bytes(), mime + "; charset=utf-8")
                    return self.send(404, dumps({"error": "页面不存在"}))
                body = self.read_body(16384 if path.startswith(("/api/extension/","/api/runtime/")) else 30_000_000)
                if path in ("/api/restore/local", "/api/restore/local/preview"):
                    body["backup"] = library.read_local_backup(body.get("backup_id"))
                    path = path.replace("/local", "")
                if path == "/api/extension/code":
                    result = bridge.pairing_code()
                elif path == "/api/extension/send":
                    result = bridge.send(body)
                elif path == "/api/extension/revoke":
                    result = bridge.revoke()
                elif path == "/api/runtime/call":
                    result=runtime.submit(body)
                elif path == "/api/runtime/cancel":
                    result=runtime.cancel(body["job_id"])
                elif path == "/api/runtime/stop-browser":
                    result=bridge.stop_browser()
                elif path == "/api/edit":
                    result = library.edit(body)
                elif path == "/api/tag":
                    result = library.manage_tag(body)
                elif path == "/api/undo":
                    result = library.undo(body)
                elif path == "/api/restore/preview":
                    result = library.restore(body, preview=True)
                elif path == "/api/backup/local":
                    result = library.save_local_backup()
                elif path == "/api/restore":
                    # 覆盖前保留本机原备份，避免错误恢复导致不可回退。
                    library.validate_backup(body.get("backup"))
                    library.save_local_backup("before-restore")
                    result = library.restore(body)
                elif path == "/api/sync":
                    result = sync.start(body)
                elif path == "/api/sync/cancel":
                    sync.cancelled.set()
                    result = True
                else:
                    return self.send(404, dumps({"error": "操作不存在"}))
                self.send(200, dumps({"ok": True, "result": result}))
            except PermissionError as exc:
                self.send(403, dumps({"error": str(exc)}))
            except PlatformError as exc:
                self.send(409 if exc.kind == "conflict" else 400, dumps({"error": str(exc), "kind": exc.kind}))
            except (ValueError, KeyError, TypeError) as exc:
                self.send(400, dumps({"error": str(exc)}))
            except (OSError, sqlite3.Error) as exc:
                self.send(500, dumps({"error": "本地读写失败，未确认保存；请保留输入并重试", "kind": type(exc).__name__}))

    class PluginServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self):
            if hasattr(self,"runtime"):
                self.runtime.close()
            super().server_close()

    server = PluginServer(("127.0.0.1", port), Handler)
    runtime=Runtime(library,bridge,sync.client)
    if port == 0:
        allowed.clear()
        allowed.update({f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"})
    server.library_sync = sync
    server.extension_bridge = bridge
    server.runtime=runtime
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="打开私人收藏库")
    parser.add_argument("--workspace", default="private/library")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--cookie-file")
    parser.add_argument("--port", type=int, default=6861)
    parser.add_argument("--trial-dir", type=Path)
    args = parser.parse_args(argv)
    library = Library(args.workspace)
    if args.dataset:
        library.seed(args.dataset, args.results, args.annotations)
    server = make_server(library, port=args.port, cookie_file=args.cookie_file, trial_dir=args.trial_dir)
    from .workspace import save_json
    save_json(library.root / "server.json", {"url": f"http://127.0.0.1:{server.server_port}",
                                          "pid": os.getpid(), "started": time.time()})
    print(f"私人收藏库已打开：http://127.0.0.1:{server.server_port} · 数据：{library.root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
