"""私人任务、缓存与操作结果；不保存平台 Cookie 或模型密钥。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .protocol import PlatformError, fingerprint


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "work.sqlite"
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS classification(
              cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage(
              id INTEGER PRIMARY KEY, run TEXT, status TEXT, reserved_input INTEGER,
              reserved_output INTEGER, actual_input INTEGER, actual_output INTEGER);
            CREATE TABLE IF NOT EXISTS operation(
              operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)

    def connect(self):
        return sqlite3.connect(self.db, timeout=30)

    def cached(self, key):
        with self.connect() as c:
            row = c.execute(
                "SELECT payload FROM classification WHERE cache_key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO classification VALUES(?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def reserve(self, run, input_upper, output_upper, input_cap, output_cap):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            used = c.execute(
                "SELECT COALESCE(SUM(reserved_input),0),"
                "COALESCE(SUM(reserved_output),0) FROM usage WHERE run=?",
                (run,),
            ).fetchone()
            if used[0] + input_upper > input_cap or used[1] + output_upper > output_cap:
                raise PlatformError("budget", "达到本次授权预算；未调用下一条")
            row = c.execute(
                "INSERT INTO usage(run,status,reserved_input,reserved_output)"
                " VALUES(?,'reserved',?,?)",
                (run, input_upper, output_upper),
            )
            return row.lastrowid

    def record_usage(self, row_id, status, prompt=None, completion=None):
        with self.connect() as c:
            c.execute(
                "UPDATE usage SET status=?,actual_input=?,actual_output=? WHERE id=?",
                (status, prompt, completion, row_id),
            )

    def operation(self, key, value=None):
        with self.connect() as c:
            if value is not None:
                c.execute(
                    "INSERT OR REPLACE INTO operation VALUES(?,?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
                return value
            row = c.execute("SELECT payload FROM operation WHERE operation_id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None


def load_ingestion(db, corpus, raw_root, folder):
    """只读已有权威资料；导出的工作集不写回摄入数据库。"""
    db, corpus, raw_root = Path(db).resolve(), Path(corpus).resolve(), Path(raw_root).resolve()
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as c:
        c.execute("PRAGMA query_only=ON")
        rows = c.execute(
            """SELECT DISTINCT d.json_path FROM canonical_document d
          JOIN source_membership m ON m.ledger_id=d.ledger_id AND m.platform=d.platform
          JOIN source_registry s ON s.source_id=m.source_id
          WHERE d.platform='bilibili' AND s.display_name=?""",
            (folder,),
        ).fetchall()
    videos = {}
    for (relative,) in rows:
        path = (corpus / relative).resolve()
        if not path.is_relative_to(corpus) or not path.is_file():
            continue
        doc = read_json(path)
        meta = doc.get("metadata", {})
        if meta.get("extraction_status") != "subtitle_complete":
            continue
        bvid, cid = meta.get("bvid"), meta.get("cid")
        if not bvid or not cid:
            continue
        segments, aid = [], None
        for raw in meta.get("raw_paths", []):
            raw_path = (raw_root / raw).resolve()
            if not raw_path.is_relative_to(raw_root) or not raw_path.is_file():
                continue
            payload = read_json(raw_path)
            if not isinstance(payload, dict):
                continue
            if payload.get("bvid") == bvid:
                aid = payload.get("id") or payload.get("aid") or aid
            if isinstance(payload.get("body"), list):
                segments = [
                    {"text": r["content"], "start": r.get("from"), "end": r.get("to")}
                    for r in payload["body"]
                    if isinstance(r, dict) and isinstance(r.get("content"), str)
                ]
        text = str(doc.get("faithful_body", "")).strip()
        if not segments:
            segments = [{"text": text, "start": None, "end": None}]
        video = videos.setdefault(
            bvid,
            {
                "bvid": bvid,
                "aid": aid,
                "type": 2,
                "title": meta.get("title", bvid),
                "author": meta.get("author", ""),
                "parts": [],
                "all_parts_verified": False,
                "evidence_origin": "local_ingestion",
            },
        )
        video["aid"] = video.get("aid") or aid
        video["parts"].append(
            {
                "cid": int(cid),
                "segments": segments,
                "source": "cached_subtitle",
                "reference": str(path),
            }
        )
    return list(videos.values())


def snapshot(client, folder_name, *, limit=0, cache=(), fetch_subtitles=False):
    folder = client.resolve_folder(folder_name)
    rows, complete = client.inventory(folder, limit=limit)
    cached = {v["bvid"]: v for v in cache}
    videos = []
    for row in rows:
        bvid = row.get("bvid")
        video = {
            "bvid": bvid,
            "aid": int(row.get("id") or row["aid"]),
            "type": int(row.get("type", 2)),
            "title": row.get("title", ""),
            "parts": [],
            "all_parts_verified": False,
        }
        if not bvid or video["type"] != 2:
            video["evidence_error"] = "unsupported_or_unavailable"
            videos.append(video)
            continue
        try:
            detail = client.video(bvid)
            if str(detail.get("aid")) != str(video["aid"]):
                raise PlatformError("identity", "收藏条目与视频详情的资源身份不符")
            pages = detail.get("pages")
            if not isinstance(pages, list) or not pages:
                raise PlatformError("transient", "视频没有有效分P列表")
            if not all(
                isinstance(p, dict) and type(p.get("cid")) is int and p["cid"] > 0 for p in pages
            ) or len({p["cid"] for p in pages}) != len(pages):
                raise PlatformError("identity", "分P列表缺少唯一有效身份")
            video["description"] = detail.get("desc", "")
            cached_parts = {p["cid"]: p for p in cached.get(bvid, {}).get("parts", [])}
            for page in pages:
                cid = int(page["cid"])
                part = cached_parts.get(cid)
                if (part is None or not part.get("identity_verified")) and fetch_subtitles:
                    part = client.transcript(bvid, cid, video["aid"])
                    if part:
                        part.pop("raw", None)
                if part:
                    video["parts"].append(part)
            video["all_parts_verified"] = len(video["parts"]) == len(pages) and all(
                p.get("identity_verified") for p in video["parts"]
            )
            video["expected_parts"] = len(pages)
        except PlatformError as exc:
            if exc.kind in ("auth", "rate_limit"):
                raise
            video["evidence_error"] = exc.kind
            video["evidence_error_detail"] = str(exc)
        videos.append(video)
    data = {
        "schema": 1,
        "account_mid": client.identity["mid"],
        "source_folder": {"id": int(folder["id"]), "title": folder["title"]},
        "inventory_complete": complete,
        "videos": videos,
    }
    data["snapshot_hash"] = fingerprint(data)
    return data
