"""插件共用的动作目录、持久任务与确定性结果，不执行模型生成的代码。"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .plugins import discover
from .protocol import PlatformError


def validate(value, schema):
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "string": isinstance(value, str),
             "integer": type(value) is int, "number": type(value) in (int, float),
             "boolean": type(value) is bool, "array": isinstance(value, list)}
    if not valid.get(kind, False):
        raise ValueError(f"参数须为{kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("参数选项无效")
    if kind == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys() or value.keys() - props.keys():
            raise ValueError("参数缺失或包含未开放字段")
        for key, item in value.items():
            validate(item, props[key])
    if kind == "string" and (len(value) > schema.get("maxLength", 4096) or len(value) < schema.get("minLength", 0)
                              or ("pattern" in schema and not re.fullmatch(schema["pattern"], value))):
        raise ValueError("文字长度或格式无效")
    if kind in ("integer", "number") and not schema.get("minimum", -1e20) <= value <= schema.get("maximum", 1e20):
        raise ValueError("数值超出范围")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ValueError("列表数量超出范围")
        for item in value:
            validate(item, schema["items"])


def object_schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


class Runtime:
    def __init__(self, library, bridge, platform_factory):
        from .video_reader import cleanup_temporary
        cleanup_temporary(library)
        self.library, self.bridge, self.platform_factory = library, bridge, platform_factory
        self.actions = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bili-plugin")
        for plugin in discover().values():
            for action in plugin.actions:
                if not action.name.startswith(plugin.name + ".") or action.name in self.actions or action.effect not in ("read", "browser", "write") or not callable(action.handler):
                    raise PlatformError("plugin", "插件动作名称或权限声明无效")
                self.actions[action.name] = action
        with library.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_jobs(
              id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL,
              arguments TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
              created REAL NOT NULL, updated REAL NOT NULL);
            """)
            c.execute("UPDATE runtime_jobs SET status='interrupted',error=?,updated=? WHERE status IN ('queued','running')",
                      (json.dumps({"kind":"interrupted", "message":"后台重启，未自动重放操作；写入须先核对结果"}, ensure_ascii=False), time.time()))
        token_file = library.root / "control-token.txt"
        if not token_file.exists():
            # 令牌文件只在忽略目录，模型不需要读取；供本机工具入口使用。
            token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        self.control_token = token_file.read_text(encoding="utf-8").strip()

    def catalog(self):
        return {"api_version":1,"actions":[{"name":a.name,"description":a.description,"parameters":a.parameters,"effect":a.effect} for a in self.actions.values()]}

    def submit(self, body):
        if not isinstance(body, dict) or set(body) != {"action", "args", "request_id"}:
            raise ValueError("动作请求须包含 action、args、request_id")
        if not isinstance(body["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", body["request_id"]):
            raise ValueError("请求ID无效")
        action = self.actions.get(body["action"])
        if not action:
            raise ValueError("未开放此插件动作")
        validate(body["args"], action.parameters)
        encoded = json.dumps(body["args"], ensure_ascii=False, sort_keys=True)
        with self.lock, self.library.connect() as c:
            row = c.execute("SELECT id,action,arguments FROM runtime_jobs WHERE request_id=?", (body["request_id"],)).fetchone()
            if row:
                if row["action"] != action.name or row["arguments"] != encoded:
                    raise ValueError("同一请求ID不能用于不同操作")
                return {"job_id":row["id"]}
            if c.execute("SELECT COUNT(*) FROM runtime_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 20:
                raise ValueError("已有20项等待任务，请先完成或取消")
            job_id = secrets.token_hex(16)
            c.execute("INSERT INTO runtime_jobs VALUES(?,?,?,?,?,?,?,?,?)", (job_id,body["request_id"],action.name,encoded,"queued",None,None,time.time(),time.time()))
        self.pool.submit(self.run, job_id)
        return {"job_id":job_id}

    def job(self, job_id):
        with self.library.connect() as c:
            row = c.execute("SELECT * FROM runtime_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        value = dict(row)
        for key in ("arguments", "result", "error"):
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def jobs(self):
        with self.library.connect() as c:
            rows=c.execute("SELECT * FROM runtime_jobs ORDER BY created DESC LIMIT 30").fetchall()
        values=[]
        for row in rows:
            value=dict(row)
            for key in ('arguments','result','error'):
                value[key]=json.loads(value[key]) if value[key] else None
            values.append(value)
        return values

    def update(self, job_id, status, *, result=None, error=None):
        with self.library.connect() as c:
            c.execute("UPDATE runtime_jobs SET status=?,result=?,error=?,updated=? WHERE id=?", (status,
                      json.dumps(result,ensure_ascii=False) if result is not None else None,
                      json.dumps(error,ensure_ascii=False) if error else None,time.time(),job_id))

    def check(self, job_id):
        if self.job(job_id)["status"] == "cancelled":
            raise PlatformError("cancelled", "任务已取消；已提交的站内写入不会伪装成回滚")

    def run(self, job_id):
        with self.lock:
            job = self.job(job_id)
            if job["status"] != "queued":
                return
            self.update(job_id,"running")
        try:
            value = self.actions[job["action"]].handler(Context(self,job_id), job["arguments"])
            if self.job(job_id)["status"] != "cancelled":
                self.update(job_id,"complete",result=value)
        except Exception as exc:  # noqa: BLE001 -- 插件任务边界须记录失败，不向用户伪报成功
            if self.job(job_id)["status"] != "cancelled":
                error = {"kind":getattr(exc,"kind","invalid" if isinstance(exc,ValueError) else "internal"),
                         "message":str(exc) if isinstance(exc,(ValueError,PlatformError)) else "执行失败，未计为成功"}
                self.update(job_id,"failed",error=error,result=self.job(job_id)["result"])

    def cancel(self, job_id):
        with self.lock:
            job = self.job(job_id)
            if job["status"] in ("queued","running"):
                self.update(job_id,"cancelled",result=job["result"],error={"kind":"cancelled","message":"已停止后续步骤，已发生的站内写入需读回核对"})
        return self.job(job_id)

    def close(self):
        self.bridge.stop_browser()
        self.pool.shutdown(wait=False,cancel_futures=True)


class Context:
    """插件共享接口v1：平台客户端、受控浏览器、任务检查与来源任务结果。"""
    api_version = 1

    def __init__(self, runtime, job_id):
        self.runtime, self.job_id = runtime, job_id

    def check(self):
        self.runtime.check(self.job_id)

    def platform(self):
        self.check()
        return self.runtime.platform_factory()

    def browser(self, action, args):
        return self.runtime.bridge.browser_request(action,args,cancelled=self.check)

    def result(self, job_id, action):
        job = self.runtime.job(job_id)
        if job["status"] != "complete" or job["action"] != action:
            raise ValueError("来源任务尚未完成或类型不符")
        return job["result"]

    def checkpoint(self, value):
        with self.runtime.library.connect() as c:
            c.execute("UPDATE runtime_jobs SET result=?,updated=? WHERE id=?", (json.dumps(value,ensure_ascii=False),time.time(),self.job_id))
