"""私人收藏库：平台元数据与人工标注分表，所有修改在 SQLite 事务中完成。"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path

from .operations import verify_snapshot
from .protocol import PlatformError
from .workspace import read_json, save_json


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def tags(value):
    if not isinstance(value, list) or len(value) > 80:
        raise ValueError("标签须为数组，每条最多80个")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 60:
            raise ValueError("标签不能为空，且不能超过60字")
        name = item.strip()
        if name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


def annotation(value):
    if not isinstance(value, dict):
        raise TypeError("标注内容无效")
    note = value.get("note", "")
    if not isinstance(note, str) or len(note) > 20000:
        raise ValueError("备注最多20000字")
    status = value.get("study", "inbox")
    if status not in ("inbox", "learning", "done"):
        raise ValueError("学习状态无效")
    if type(value.get("archived", False)) is not bool:
        raise ValueError("归档状态无效")
    return {"tags": tags(value.get("tags", [])), "note": note,
            "study": status, "archived": value.get("archived", False)}


class Library:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "library.sqlite"
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES('revision','0');
            CREATE TABLE IF NOT EXISTS videos(bvid TEXT PRIMARY KEY COLLATE BINARY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS annotations(bvid TEXT PRIMARY KEY COLLATE BINARY,
              payload TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,label TEXT NOT NULL,
              before_data TEXT NOT NULL,after_data TEXT NOT NULL,created REAL NOT NULL,undone INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            """)

    def connect(self):
        c = sqlite3.connect(self.db, timeout=20)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _meta(c, key):
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _set(c, key, value):
        c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dumps(value)))

    def _account(self, c, mid):
        if type(mid) is not int or mid <= 0:
            raise ValueError("快照缺少已核对账号")
        known = self._meta(c, "account")
        if known is not None and known != mid:
            raise PlatformError("identity", "当前账号与私人库不同；请使用各自独立的库")
        self._set(c, "account", mid)

    def _bump(self, c):
        self._set(c, "revision", self._meta(c, "revision") + 1)

    def account(self):
        with self.connect() as c:
            return self._meta(c, "account")

    def seed(self, dataset_path, results_path=None, annotations_path=None):
        data = read_json(dataset_path)
        verify_snapshot(data)
        judgments = read_json(results_path) if results_path else {}
        if judgments and judgments.get("snapshot_hash") != data["snapshot_hash"]:
            raise ValueError("内容判断不属于该快照")
        results = {r["bvid"]: r for r in judgments.get("results", [])}
        initial = read_json(annotations_path) if annotations_path else {}
        if initial and (initial.get("account_mid") != data["account_mid"] or
                        initial.get("snapshot_hash") != data["snapshot_hash"]):
            raise ValueError("初始私人标注不属于当前账号和快照")
        initial_items = initial.get("annotations", {})
        if not isinstance(initial_items, dict) or set(initial_items) - {v.get("bvid") for v in data["videos"]}:
            raise ValueError("初始私人标注包含范围外视频")
        initial_items = {k: annotation(v) for k, v in initial_items.items()}
        with self.connect() as c:
            if self._meta(c, "seeded"):
                return False
            c.execute("BEGIN IMMEDIATE")
            self._account(c, data["account_mid"])
            source = {**data["source_folder"], "complete": data["inventory_complete"],
                      "scope": "fixed", "count": len(data["videos"]), "updated": time.time()}
            c.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (source["id"], dumps(source)))
            for video in data["videos"]:
                bvid = video.get("bvid")
                if not bvid:
                    continue
                payload = {**video, "folders": [source["id"]], "judgment": results.get(bvid, {}),
                           "synced": None, "availability": "cached"}
                c.execute("INSERT OR IGNORE INTO videos VALUES(?,?)", (bvid, dumps(payload)))
                # 只消费明确给出的私人标注；用户用途和视频ID不写进公共源码。
                value = initial_items.get(bvid, annotation({}))
                c.execute("INSERT OR IGNORE INTO annotations VALUES(?,?,?)",
                          (bvid, dumps(value), time.time()))
            self._set(c, "seeded", data["snapshot_hash"])
            self._bump(c)
        return True

    def state(self):
        with self.connect() as c:
            notes = {r["bvid"]: {**json.loads(r["payload"]), "updated": r["updated"]}
                     for r in c.execute("SELECT * FROM annotations")}
            videos = [{**json.loads(r["payload"]), "annotation": notes.get(r["bvid"], annotation({}))}
                      for r in c.execute("SELECT * FROM videos ORDER BY rowid")]
            sources = [json.loads(r["payload"]) for r in c.execute("SELECT * FROM sources")]
            history = [dict(r) for r in c.execute(
                "SELECT id,label,created,undone FROM history ORDER BY id DESC LIMIT 30")]
            job = c.execute("SELECT payload FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            return {"account": self._meta(c, "account"), "revision": self._meta(c, "revision"),
                    "videos": videos, "sources": sources, "history": history,
                    "job": json.loads(job[0]) if job else None}

    @staticmethod
    def _annotations(c, bvids):
        result = {}
        for bvid in bvids:
            if not c.execute("SELECT 1 FROM videos WHERE bvid=?", (bvid,)).fetchone():
                raise ValueError("所选视频不存在")
            row = c.execute("SELECT payload FROM annotations WHERE bvid=?", (bvid,)).fetchone()
            result[bvid] = json.loads(row[0]) if row else annotation({})
        return result

    def _change(self, label, revision, transform):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if revision != self._meta(c, "revision"):
                raise PlatformError("conflict", "数据已在其他窗口更新；刷新后再保存，当前输入仍保留")
            before, after = transform(c)
            if before == after:
                return False
            for bvid, value in after.items():
                c.execute("INSERT OR REPLACE INTO annotations VALUES(?,?,?)",
                          (bvid, dumps(annotation(value)), time.time()))
            c.execute("INSERT INTO history(label,before_data,after_data,created) VALUES(?,?,?,?)",
                      (label, dumps(before), dumps(after), time.time()))
            self._bump(c)
        return True

    def edit(self, body):
        bvids = body.get("bvids", [])
        if not isinstance(bvids, list) or not bvids or len(bvids) > 1000 or any(
            not isinstance(v, str) for v in bvids
        ):
            raise ValueError("请选择1至1000条视频")
        patch = body.get("patch", {})
        if not isinstance(patch, dict) or set(patch) - {"tags", "add_tags", "remove_tags", "note", "study", "archived"}:
            raise ValueError("标注字段无效")
        if len(bvids) > 1 and "note" in patch:
            raise ValueError("请逐条编辑私人备注，避免批量覆盖")

        def transform(c):
            before = self._annotations(c, bvids)
            after = copy.deepcopy(before)
            for value in after.values():
                for key in ("tags", "note", "study", "archived"):
                    if key in patch:
                        value[key] = patch[key]
                if "add_tags" in patch:
                    value["tags"] = tags(value["tags"] + tags(patch["add_tags"]))
                if "remove_tags" in patch:
                    removed = {x.casefold() for x in tags(patch["remove_tags"])}
                    value["tags"] = [t for t in value["tags"] if t.casefold() not in removed]
                annotation(value)
            return before, after
        return self._change("编辑标注" if len(bvids) == 1 else "批量编辑标注", body.get("revision"), transform)

    def manage_tag(self, body):
        old = tags([body.get("old")])[0]
        new = tags([body["new"]])[0] if body.get("new") else None

        def transform(c):
            rows = {r["bvid"]: json.loads(r["payload"]) for r in c.execute("SELECT * FROM annotations")}
            before = {k: v for k, v in rows.items() if any(t.casefold() == old.casefold() for t in v["tags"])}
            after = copy.deepcopy(before)
            for value in after.values():
                value["tags"] = tags([new if t.casefold() == old.casefold() else t
                                      for t in value["tags"] if new or t.casefold() != old.casefold()])
            return before, after
        return self._change("重命名标签" if new else "移除标签", body.get("revision"), transform)

    def undo(self, body):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if body.get("revision") != self._meta(c, "revision"):
                raise PlatformError("conflict", "库已更新，请刷新后撤销")
            row = c.execute("SELECT * FROM history WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                raise ValueError("没有可撤销的标注操作")
            before, after = json.loads(row["before_data"]), json.loads(row["after_data"])
            if self._annotations(c, after) != after:
                raise PlatformError("conflict", "相关标注已变化，不能覆盖新内容")
            for bvid, value in before.items():
                c.execute("INSERT OR REPLACE INTO annotations VALUES(?,?,?)", (bvid, dumps(value), time.time()))
            c.execute("UPDATE history SET undone=1 WHERE id=?", (row["id"],))
            self._bump(c)
        return row["label"]

    def backup(self):
        state = self.state()
        return {"app": "bili-platform-library", "schema": 1, "account": state["account"],
                "exported": time.time(), "videos": state["videos"], "sources": state["sources"]}

    def save_local_backup(self, prefix="library-backup"):
        directory = self.root / "backups"
        name = f"{prefix}-{time.time_ns()}.json"
        save_json(directory / name, self.backup())
        return {"id": name, "created": time.time()}

    def local_backups(self):
        directory = self.root / "backups"
        result = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json"), reverse=True):
                if path.name.startswith(("library-backup-", "before-restore-")):
                    result.append({"id": path.name, "created": path.stat().st_mtime,
                                   "size": path.stat().st_size,
                                   "automatic": path.name.startswith("before-restore-")})
        return sorted(result, key=lambda r: r["created"], reverse=True)

    def read_local_backup(self, name):
        if not isinstance(name, str) or Path(name).name != name or name not in {
            row["id"] for row in self.local_backups()
        }:
            raise ValueError("请选择工具已保存的本机备份")
        return read_json(self.root / "backups" / name)

    def validate_backup(self, data):
        if not isinstance(data, dict) or data.get("app") != "bili-platform-library" or data.get("schema") != 1:
            raise ValueError("不是本工具支持的收藏库备份")
        if type(data.get("account")) is not int or data["account"] <= 0:
            raise ValueError("备份账号无效")
        if self.account() is not None and data.get("account") != self.account():
            raise PlatformError("identity", "备份账号与当前库不同")
        rows = data.get("videos")
        if not isinstance(rows, list) or len(rows) > 100000:
            raise ValueError("备份视频列表无效")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("bvid"), str) or not row["bvid"].startswith("BV") or row["bvid"] in seen:
                raise ValueError("备份视频身份重复或无效")
            seen.add(row["bvid"])
            annotation(row.get("annotation"))
            if not isinstance(row.get("title"), str) or not isinstance(row.get("folders", []), list):
                raise TypeError("备份视频元数据无效")
        if not isinstance(data.get("sources"), list) or any(
            not isinstance(s, dict) or type(s.get("id")) is not int or not isinstance(s.get("title"), str)
            for s in data["sources"]
        ):
            raise ValueError("备份来源无效")
        return rows

    def restore(self, body, *, preview=False):
        data = body.get("backup")
        rows = self.validate_backup(data)
        mode = body.get("mode", "merge")
        if mode not in ("merge", "overwrite"):
            raise ValueError("恢复模式无效")
        current = {v["bvid"]: v for v in self.state()["videos"]}
        result = {"videos": len(rows), "new": sum(r["bvid"] not in current for r in rows),
                  "conflicts": sum(r["bvid"] in current and annotation(r["annotation"]) != annotation(current[r["bvid"]]["annotation"]) for r in rows), "mode": mode}
        if preview:
            return result
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if body.get("revision") != self._meta(c, "revision"):
                raise PlatformError("conflict", "库已更新，请重新预览恢复")
            self._account(c, data["account"])
            before, after = {}, {}
            for row in rows:
                bvid = row["bvid"]
                c.execute("INSERT OR IGNORE INTO videos VALUES(?,?)",
                          (bvid, dumps({k: v for k, v in row.items() if k != "annotation"})))
                old = self._annotations(c, [bvid])[bvid]
                incoming = annotation(row["annotation"])
                if mode == "merge" and bvid in current:
                    value = {**old, "tags": tags(old["tags"] + incoming["tags"]),
                             "note": old["note"] or incoming["note"]}
                else:
                    value = incoming
                before[bvid], after[bvid] = old, value
                c.execute("INSERT OR REPLACE INTO annotations VALUES(?,?,?)", (bvid, dumps(value), time.time()))
            for source in data["sources"]:
                c.execute("INSERT OR IGNORE INTO sources VALUES(?,?)", (source["id"], dumps(source)))
            c.execute("INSERT INTO history(label,before_data,after_data,created) VALUES(?,?,?,?)",
                      ("从备份恢复标注", dumps(before), dumps(after), time.time()))
            self._bump(c)
        return result

    def update_metadata(self, mid, updates, source=None):
        """同步只更新平台字段；不访问 annotations 表，不根据不完整清单删除条目。"""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._account(c, mid)
            if source:
                c.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (source["id"], dumps(source)))
            for item in updates:
                bvid = item["bvid"]
                old = c.execute("SELECT payload FROM videos WHERE bvid=?", (bvid,)).fetchone()
                payload = json.loads(old[0]) if old else {"bvid": bvid, "parts": [], "judgment": {}}
                if payload.get("aid") and item.get("aid") and payload["aid"] != item["aid"]:
                    raise PlatformError("identity", "同步视频身份与原记录不符")
                previous_folders = payload.get("folders", [])
                payload.update(item)
                if source:
                    payload["folders"] = sorted(set(previous_folders + item.get("folders", []) + [source["id"]]))
                c.execute("INSERT INTO videos VALUES(?,?) ON CONFLICT(bvid) DO UPDATE "
                          "SET payload=excluded.payload", (bvid, dumps(payload)))
            self._bump(c)

    def job(self, value=None, job_id=None):
        with self.connect() as c:
            if value is None:
                row = c.execute("SELECT id,payload FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
                return {**json.loads(row["payload"]), "id": row["id"]} if row else None
            if job_id:
                c.execute("UPDATE jobs SET payload=? WHERE id=?", (dumps(value), job_id))
            else:
                job_id = c.execute("INSERT INTO jobs(payload) VALUES(?)", (dumps(value),)).lastrowid
            return job_id
