"""扩展配对、有界消息与已授权标签页操作；不开放任意脚本或通用 RPC。"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")
MESSAGE_ID = re.compile(r"[a-zA-Z0-9_-]{8,80}\Z")


def page_payload(value):
    if not isinstance(value, dict) or set(value) != {"url", "title", "bvid"}:
        raise ValueError("页面消息结构无效")
    raw, title = value["url"], value["title"]
    if not isinstance(raw, str) or len(raw) > 4096 or not isinstance(title, str) or len(title) > 500:
        raise ValueError("页面地址或标题过长")
    url = urlsplit(raw)
    if (url.scheme != "https" or url.hostname not in ("www.bilibili.com", "space.bilibili.com")
            or url.username or url.password or url.port not in (None, 443)):
        raise ValueError("只接受当前B站视频页或收藏页")
    match = re.fullmatch(r"/video/(BV[a-zA-Z0-9]{10})/?", url.path)
    bvid = match[1] if match else None
    if not match and not (url.hostname == "space.bilibili.com" and re.fullmatch(r"/\d+/favlist/?", url.path)):
        raise ValueError("请在B站视频页或收藏页发送")
    if url.hostname != "www.bilibili.com" and match:
        raise ValueError("视频地址无效")
    if value["bvid"] != bvid:
        raise ValueError("BVID与页面地址不一致")
    # 不保存推广、来源或其他查询参数；收藏夹只保留具体 fid。
    clean = f"https://{url.hostname}{url.path}"
    if not bvid:
        from urllib.parse import parse_qs
        fid = parse_qs(url.query).get("fid", [""])[0]
        if fid.isdigit():
            clean += "?fid=" + fid
    return {"url": clean, "title": title.strip(), "bvid": bvid}


def payload(value, *, outgoing=False):
    if not isinstance(value, dict) or set(value) != {"type", "data"}:
        raise ValueError("只接受文字或当前页面消息")
    if value["type"] == "text":
        if not isinstance(value["data"], str) or not 0 < len(value["data"].strip()) <= 500:
            raise ValueError("消息须为1至500字")
        return {"type": "text", "data": value["data"].strip()}
    if value["type"] == "page" and not outgoing:
        return {"type": "page", "data": page_payload(value["data"])}
    raise ValueError("未开放此类消息或操作")


def browser_url(raw):
    if not isinstance(raw,str) or len(raw)>4096:
        raise ValueError("B站页面地址无效")
    url=urlsplit(raw)
    if (url.scheme != "https" or url.hostname not in ("www.bilibili.com","space.bilibili.com")
            or url.username or url.password or url.port not in (None,443)):
        raise ValueError("浏览器操作限B站视频、首页和收藏页")
    if url.hostname == "www.bilibili.com" and (url.path == "/" or re.fullmatch(r"/video/BV[A-Za-z0-9]{10}/?",url.path)):
        return f"https://www.bilibili.com{url.path}"
    if url.hostname == "space.bilibili.com" and re.fullmatch(r"/\d+/favlist/?",url.path):
        return page_payload({"url":raw,"title":"","bvid":None})["url"]
    raise ValueError("浏览器操作限B站视频、首页和收藏页")


class ExtensionBridge:
    def __init__(self, library, *, clock=time.time):
        self.library, self.clock = library, clock
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.binding = None
        self.commands = {}
        self.stopped_sessions = set()
        self.pairing = None
        self.seen = {}
        self.waiting = {}
        self.instance = secrets.token_hex(8)
        with library.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS extension_clients(
              key TEXT PRIMARY KEY, origin TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS extension_messages(
              seq INTEGER PRIMARY KEY, client TEXT NOT NULL, message_id TEXT NOT NULL,
              direction TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
              received INTEGER NOT NULL DEFAULT 0, UNIQUE(client,message_id));
            """)

    def pairing_code(self):
        with self.lock:
            self.pairing = {"code": secrets.token_hex(5).upper(), "expires": self.clock() + 600, "tries": 0}
            return {"code": self.pairing["code"], "expires_in": 600}

    def pair(self, origin, code):
        if not ORIGIN.fullmatch(origin or ""):
            raise PermissionError("仅允许浏览器扩展配对")
        with self.lock:
            p = self.pairing
            if not p or p["expires"] <= self.clock() or p["tries"] >= 20:
                raise PermissionError("配对码已过期，请在工作台重新生成")
            p["tries"] += 1
            if not isinstance(code, str) or not secrets.compare_digest(code.strip().upper(), p["code"]):
                raise PermissionError("配对码错误")
            token = secrets.token_urlsafe(32)
            key = hashlib.sha256(token.encode()).hexdigest()
            with self.library.connect() as c:
                # 仅允许一个当前连接；重新配对轮换凭据，保留既有消息记录。
                c.execute("DELETE FROM extension_clients")
                c.execute("INSERT INTO extension_clients VALUES(?,?,?)", (key, origin, self.clock()))
            self.pairing = None
            self.seen.clear()
            self.stop_browser()
            self.binding = None
            self.seen[key] = self.clock()
            return {"token": token, "instance": self.instance}

    def authenticate(self, origin, token):
        if not ORIGIN.fullmatch(origin or "") or not isinstance(token, str) or len(token) > 200:
            raise PermissionError("配对已失效，请重新配对")
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.library.connect() as c:
            row = c.execute("SELECT origin FROM extension_clients WHERE key=?", (key,)).fetchone()
        if not row or row["origin"] != origin:
            raise PermissionError("配对已失效，请重新配对")
        return key

    def _message(self, c, key, body, direction):
        if not isinstance(body, dict) or set(body) != {"id", "payload"}:
            raise ValueError("消息结构无效")
        mid = body["id"]
        if not isinstance(mid, str) or not MESSAGE_ID.fullmatch(mid):
            raise ValueError("消息ID无效")
        value = payload(body["payload"], outgoing=direction == "to_extension")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        old = c.execute("SELECT payload,direction FROM extension_messages WHERE client=? AND message_id=?", (key, mid)).fetchone()
        if old and (old["payload"] != encoded or old["direction"] != direction):
            raise ValueError("重复消息ID的内容不一致")
        c.execute("INSERT OR IGNORE INTO extension_messages(client,message_id,direction,payload,created,received) VALUES(?,?,?,?,?,?)",
                  (key, mid, direction, encoded, self.clock(), int(direction == "to_workbench")))
        return {"id": mid, "saved": True}

    def extension_request(self, origin, token, action, body):
        wait=body.get('wait',0) if action=='poll' and isinstance(body,dict) else 0
        if type(wait) is not int or not 0<=wait<=20:
            raise ValueError('等待时长无效')
        if action!='poll' or not wait:
            clean={k:v for k,v in body.items() if k!='wait'} if action=='poll' else body
            return self._extension_request(origin,token,action,clean)
        clean={k:v for k,v in body.items() if k!='wait'}
        with self.condition:
            key=self.authenticate(origin,token)
            first=self._extension_request(origin,token,action,clean)
            if first['command'] or first['messages'] or first['stopped']:
                return first
            session=(self.binding or {}).get('session')
            lease=secrets.token_hex(8)
            self.waiting[lease]=(key,self.clock()+wait+1)
            try:
                self.condition.wait_for(lambda: any(not c.get('sent') and not c.get('error') for c in self.commands.values())
                                        or key not in self.seen
                                        or bool(self.binding and not self.binding['control'] and (clean.get('binding') or {}).get('control')),
                                        timeout=wait)
                self.authenticate(origin,token)
                if (self.binding or {}).get('session')!=session:
                    return {'messages':[],'instance':self.instance,'command':None,'stopped':False}
                # 等待期间可能收到新的绑定；绝不能把旧请求的绑定重新写回。
                return self._extension_request(origin,token,action,{'acks':[]})
            finally:
                self.waiting.pop(lease,None)

    def online(self,key):
        return self.clock()-self.seen.get(key,0)<8 or any(k==key and expiry>self.clock() for k,expiry in self.waiting.values())

    def _extension_request(self, origin, token, action, body):
        with self.lock, self.library.connect() as c:
            key = self.authenticate(origin, token)
            if action == "message":
                result = self._message(c, key, body, "to_workbench")
            elif action == "poll":
                acks = body.get("acks", [])
                if set(body) - {"acks","binding"} or not isinstance(acks, list) or len(acks) > 100 or any(
                    not isinstance(a, str) or not MESSAGE_ID.fullmatch(a) for a in acks
                ):
                    raise ValueError("消息回执无效")
                for mid in acks:
                    c.execute("UPDATE extension_messages SET received=1 WHERE client=? AND message_id=? AND direction='to_extension'", (key, mid))
                rows = c.execute("SELECT message_id,payload,created FROM extension_messages WHERE client=? AND direction='to_extension' AND received=0 ORDER BY seq LIMIT 50", (key,)).fetchall()
                if body.get("binding") is not None:
                    binding=body["binding"]
                    if (not isinstance(binding,dict) or set(binding) != {"tab_id","session","control","url"}
                            or type(binding["tab_id"]) is not int or binding["tab_id"]<=0
                            or not isinstance(binding["session"],str) or not re.fullmatch(r"[a-f0-9-]{36}",binding["session"])
                            or type(binding["control"]) is not bool):
                        raise ValueError("浏览器标签页绑定无效")
                    safe={**binding,"url":browser_url(binding["url"]),"key":key}
                    if safe["session"] in self.stopped_sessions:
                        safe["control"]=False
                    if self.binding and (safe["session"] != self.binding["session"] or not safe["control"]):
                        for command in self.commands.values():
                            command["error"]="用户停止或重新绑定了标签页"
                    self.binding=safe
                elif "binding" in body:
                    self.stop_browser()
                    self.binding=None
                command=None
                if self.binding and self.binding["key"] == key and self.binding["control"]:
                    pending=next((v for v in self.commands.values() if not v.get("sent") and not v.get("error")
                                  and v["session"] == self.binding["session"] and v["expires"]>self.clock()),None)
                    if pending:
                        pending["sent"]=True
                        command={k:pending[k] for k in ("id","session","tab_id","action","args","expires")}
                result = {"messages": [{"id": r["message_id"], "payload": json.loads(r["payload"]), "created": r["created"]} for r in rows],
                          "instance": self.instance,"command":command,
                          "stopped":bool(self.binding and self.binding["session"] in self.stopped_sessions)}
                self.condition.notify_all()
            elif action == "result":
                if set(body) != {"id","session","result","error"}:
                    raise ValueError("浏览器结果结构无效")
                command=self.commands.get(body["id"])
                if (not command or command["key"] != key or command["session"] != body["session"]
                        or command.get("error") or command["expires"]<=self.clock()
                        or not self.binding or self.binding["session"] != body["session"] or not self.binding["control"]):
                    raise ValueError("浏览器操作已失效，结果未接入")
                if body["error"] is not None and not isinstance(body["error"],str):
                    raise ValueError("浏览器错误信息无效")
                command.update(done=True,result=body["result"],error=body["error"])
                self.condition.notify_all()
                result={"accepted":True}
            else:
                raise ValueError("未开放此连接操作")
            self.seen[key] = self.clock()
            return result

    def send(self, body):
        with self.lock, self.library.connect() as c:
            row = c.execute("SELECT key FROM extension_clients LIMIT 1").fetchone()
            if not row:
                raise ValueError("请先配对扩展")
            count = c.execute("SELECT COUNT(*) FROM extension_messages WHERE client=? AND direction='to_extension' AND received=0", (row["key"],)).fetchone()[0]
            duplicate = c.execute("SELECT 1 FROM extension_messages WHERE client=? AND message_id=?",
                                  (row["key"], body.get("id"))).fetchone()
            if count >= 50 and not duplicate:
                raise ValueError("未送达消息已达50条，请先连接扩展")
            return self._message(c, row["key"], body, "to_extension")

    def revoke(self):
        self.stop_browser()
        with self.lock, self.library.connect() as c:
            c.execute("DELETE FROM extension_clients")
            self.pairing = None
            self.seen.clear()
        return {"revoked": True}

    def state(self):
        with self.lock, self.library.connect() as c:
            client = c.execute("SELECT key,origin,created FROM extension_clients LIMIT 1").fetchone()
            rows = c.execute("SELECT message_id,direction,payload,created,received FROM extension_messages ORDER BY seq DESC LIMIT 50").fetchall()
            return {"paired": bool(client), "online": bool(client and self.online(client['key'])),
                    "last_seen": self.seen.get(client["key"]) if client else None,
                    "instance": self.instance, "origin": client["origin"] if client else None,
                    "browser":{k:v for k,v in self.binding.items() if k != "key"} if client and self.binding else None,
                    "messages": [{"id": r["message_id"], "direction": r["direction"], "payload": json.loads(r["payload"]),
                                  "created": r["created"], "received": bool(r["received"])} for r in reversed(rows)]}

    def stop_browser(self):
        with self.condition:
            if self.binding:
                self.stopped_sessions.add(self.binding["session"])
                self.binding["control"]=False
            for command in self.commands.values():
                command["error"]="用户已停止浏览器操作"
            self.condition.notify_all()
        return {"stopped":True}

    def browser_request(self,action,args,*,cancelled=None,timeout=30):
        from .protocol import PlatformError
        if action not in ("snapshot","navigate","scroll","click","player"):
            raise ValueError("未开放此浏览器动作")
        if action == "navigate":
            args={**args,"url":browser_url(args["url"])}
        with self.condition:
            b=self.binding
            if not b or not b["control"] or not self.online(b['key']):
                raise PlatformError("browser_offline","请打开扩展工作面板，绑定一个B站标签页并开启操作")
            command={"id":secrets.token_hex(16),"key":b["key"],"session":b["session"],"tab_id":b["tab_id"],
                     "action":action,"args":args,"expires":self.clock()+timeout}
            self.commands[command["id"]]=command
            self.condition.notify_all()
            try:
                while not command.get("done") and not command.get("error"):
                    if cancelled:
                        cancelled()
                    if self.clock()>=command["expires"]:
                        raise PlatformError("browser_timeout","浏览器没有在时限内返回结果；未自动重发操作")
                    self.condition.wait(.2)
                if command.get("error"):
                    raise PlatformError("browser_action",str(command["error"])[:1000])
                value=command.get("result")
                if not isinstance(value,dict) or value.get("tab_id") != b["tab_id"] or not isinstance(value.get("page"),dict):
                    raise PlatformError("identity","浏览器未返回实际页面身份")
                browser_url(value["page"].get("url"))
                if value.get("action") != action:
                    raise PlatformError("identity","浏览器返回的动作类型不符")
                if action == "navigate" and browser_url(value["page"]["url"]) != browser_url(args["url"]):
                    raise PlatformError("identity","浏览器没有到达请求页面")
                return value
            finally:
                self.commands.pop(command["id"],None)
