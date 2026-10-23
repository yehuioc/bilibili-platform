import copy
import json
import threading

import httpx
import pytest

from bili_platform.library import Library, annotation
from bili_platform.protocol import PlatformError, fingerprint
from bili_platform.web import Sync, make_server
from bili_platform.workspace import save_json


@pytest.fixture
def library(tmp_path):
    data = {"schema": 1, "account_mid": 42, "source_folder": {"id": 7, "title": "固定样本"},
            "inventory_complete": False, "videos": [
                {"bvid": "BV1AbCdEfGhJ", "aid": 1, "title": "构图概念", "description": "平台简介"},
                {"bvid": "BV1abcdefghj", "aid": 2, "title": "另一视频", "description": "另一简介"},
            ]}
    data["snapshot_hash"] = fingerprint(data)
    save_json(tmp_path / "dataset.json", data)
    lib = Library(tmp_path / "library")
    lib.seed(tmp_path / "dataset.json")
    return lib


def edit(lib, bvids=None, **patch):
    return lib.edit({"revision": lib.state()["revision"],
                     "bvids": bvids or ["BV1AbCdEfGhJ"], "patch": patch})


def test_restart_sync_and_case_sensitive_identity(library):
    edit(library, tags=["ExampleSkill", "构图参考"], note="为下周练习做准备", study="learning")
    reopened = Library(library.root)
    assert reopened.state()["videos"][0]["annotation"]["note"] == "为下周练习做准备"
    reopened.update_metadata(42, [{"bvid": "BV1AbCdEfGhJ", "aid": 1,
                                   "title": "标题已修改", "description": "最新平台简介"}])
    rows = reopened.state()["videos"]
    assert len(rows) == 2
    assert rows[0]["annotation"]["tags"] == ["ExampleSkill", "构图参考"]
    assert rows[0]["annotation"]["note"] == "为下周练习做准备"
    assert rows[1]["annotation"]["tags"] == []


def test_metadata_cannot_mutate_private_fields(library):
    edit(library, tags=["私人标签"], note="私人用途")
    library.update_metadata(42, [{"bvid": "BV1AbCdEfGhJ", "aid": 1, "description": "远端刷新"}])
    assert library.state()["videos"][0]["annotation"]["note"] == "私人用途"
    with pytest.raises(PlatformError):
        library.update_metadata(43, [{"bvid": "BV1AbCdEfGhJ", "title": "错账号"}])
    with pytest.raises(PlatformError):
        library.update_metadata(42, [{"bvid": "BV1AbCdEfGhJ", "aid": 999}])


def test_bulk_edit_is_atomic_and_conflicting_edit_does_not_overwrite(library):
    old_revision = library.state()["revision"]
    edit(library, note="必须保留")
    with pytest.raises(PlatformError, match="其他窗口"):
        library.edit({"revision": old_revision, "bvids": ["BV1AbCdEfGhJ"], "patch": {"note": "旧窗口"}})
    with pytest.raises(ValueError):
        edit(library, bvids=["BV1AbCdEfGhJ", "missing"], add_tags=["不应该保存"])
    assert library.state()["videos"][0]["annotation"]["tags"] == []
    assert library.state()["videos"][0]["annotation"]["note"] == "必须保留"


def test_rename_merge_remove_and_undo_preserve_notes(library):
    edit(library, tags=["ExampleSkill", "构图"], note="我的用途")
    edit(library, bvids=["BV1abcdefghj"], tags=["ExampleSkill"])
    library.manage_tag({"revision": library.state()["revision"], "old": "ExampleSkill", "new": "构图"})
    assert [v["annotation"]["tags"] for v in library.state()["videos"]] == [["构图"], ["构图"]]
    library.undo({"revision": library.state()["revision"]})
    assert library.state()["videos"][0]["annotation"]["tags"] == ["ExampleSkill", "构图"]
    library.manage_tag({"revision": library.state()["revision"], "old": "构图"})
    assert library.state()["videos"][0]["annotation"]["tags"] == ["ExampleSkill"]
    assert library.state()["videos"][0]["annotation"]["note"] == "我的用途"


def test_backup_roundtrip_merge_and_overwrite_undo(library, tmp_path):
    edit(library, tags=["ExampleSkill"], note="原始备注", study="learning", archived=True)
    backup = json.loads(json.dumps(library.backup(), ensure_ascii=False))
    edit(library, tags=["新标签"], note="新的备注", study="done", archived=False)
    preview = library.restore({"backup": backup, "mode": "overwrite"}, preview=True)
    assert preview["conflicts"] == 1
    library.restore({"backup": backup, "revision": library.state()["revision"], "mode": "merge"})
    current = library.state()["videos"][0]["annotation"]
    assert current["note"] == "新的备注" and current["tags"] == ["新标签", "ExampleSkill"]
    library.restore({"backup": backup, "revision": library.state()["revision"], "mode": "overwrite"})
    current = library.state()["videos"][0]["annotation"]
    assert current["note"] == "原始备注" and current["archived"]
    library.undo({"revision": library.state()["revision"]})
    assert library.state()["videos"][0]["annotation"]["note"] == "新的备注"
    fresh = Library(tmp_path / "fresh")
    fresh.update_metadata(42, [])
    fresh.restore({"backup": backup, "revision": fresh.state()["revision"], "mode": "overwrite"})
    assert fresh.backup()["videos"] == backup["videos"] or all(
        annotation(a["annotation"]) == annotation(b["annotation"])
        for a, b in zip(fresh.state()["videos"], backup["videos"], strict=True)
    )


@pytest.mark.parametrize("change", ["duplicate", "account", "note", "tags", "sources"])
def test_invalid_restore_leaves_everything_unchanged(library, change):
    backup = library.backup()
    bad = copy.deepcopy(backup)
    if change == "duplicate":
        bad["videos"].append(copy.deepcopy(bad["videos"][0]))
    elif change == "account":
        bad["account"] = 1
    elif change == "note":
        bad["videos"][1]["annotation"]["note"] = ["错误结构"]
    elif change == "tags":
        bad["videos"][1]["annotation"]["tags"] = [None]
    else:
        bad["sources"] = ["bad"]
    with pytest.raises((ValueError, TypeError, PlatformError)):
        library.restore({"backup": bad, "revision": library.state()["revision"], "mode": "overwrite"})
    after = library.backup()
    assert after["videos"] == backup["videos"] and after["sources"] == backup["sources"]


def test_http_local_session_csrf_and_invalid_json(library):
    server = make_server(library, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=url, trust_env=False) as http:
            assert http.get("/api/state").status_code == 403
            assert http.get("/").status_code == 200
            state = http.get("/api/state").json()
            assert state["account"] == 42
            assert http.get("/api/state", headers={"Host": "attacker.invalid"}).status_code == 403
            assert http.post("/api/edit", json={}).status_code == 403
            headers = {"X-Library-Token": state["token"]}
            assert http.post("/api/edit", json={}, headers={**headers, "Origin": "https://attacker.invalid"}).status_code == 403
            assert http.post("/api/edit", content="broken", headers={**headers, "Content-Type": "application/json"}).status_code == 400
            assert http.post("/api/edit", json={"revision": state["revision"], "bvids": ["BV1AbCdEfGhJ"], "patch": {"note": "HTTP保存"}}, headers=headers).status_code == 200
            exported = http.get("/api/backup").json()
            assert exported["videos"][0]["annotation"]["note"] == "HTTP保存"
            assert "token" not in exported and "cookie" not in json.dumps(exported).lower()
            assert http.get("/../library.sqlite").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_sync_wrong_account_stops_without_changes(library, monkeypatch):
    class WrongClient:
        def nav(self):
            return {"mid": 99}

        def close(self):
            pass
    cookie = library.root / "fake-cookie.json"
    cookie.write_text("[]")
    monkeypatch.setattr("bili_platform.web.BilibiliClient.from_cookie_file", lambda _: WrongClient())
    sync = Sync(library, cookie)
    before = library.backup()["videos"]
    with pytest.raises(PlatformError, match="账号"):
        sync.client()
    assert library.backup()["videos"] == before


def test_import_preserves_other_folder_links_and_private_annotations(library):
    edit(library, tags=["跨夹检索"], note="两个收藏来源指向同一视频")
    library.update_metadata(42, [{"bvid": "BV1AbCdEfGhJ", "aid": 1, "folders": [8]}],
                            {"id": 8, "title": "第二个来源"})
    video = library.state()["videos"][0]
    assert video["folders"] == [7, 8]
    assert video["annotation"]["tags"] == ["跨夹检索"]
    assert video["annotation"]["note"] == "两个收藏来源指向同一视频"


def test_local_backup_is_scoped_to_owned_files(library):
    saved = library.save_local_backup()
    assert saved["id"] in {row["id"] for row in library.local_backups()}
    assert library.read_local_backup(saved["id"])["videos"] == library.backup()["videos"]
    for name in ("../dataset.json", str(library.db), "arbitrary.json", None):
        with pytest.raises(ValueError, match="工具已保存"):
            library.read_local_backup(name)


def test_fresh_import_binds_account_before_detail_updates(tmp_path, monkeypatch):
    class FreshClient:
        def __init__(self):
            self.identity = {"mid": 42}

        def folders(self):
            return [{"id": 7, "title": "来源"}]

        def inventory(self, folder, *, limit):
            return [{"bvid": "BV1AbCdEfGhJ", "id": 1, "title": "清单标题"}], True

        def video(self, bvid):
            return {"bvid": bvid, "aid": 1, "title": "详细标题", "duration": 60}

        def close(self):
            pass

    lib = Library(tmp_path / "fresh-import")
    sync = Sync(lib, None)
    monkeypatch.setattr(sync, "client", FreshClient)
    job = {"kind": "import", "status": "running", "done": 0, "errors": []}
    job_id = lib.job(job)
    sync.lock.acquire()
    sync.run({"folder_id": 7, "limit": 20}, job, job_id)
    assert lib.job()["status"] == "complete"
    assert lib.account() == 42
    assert lib.state()["videos"][0]["title"] == "详细标题"
