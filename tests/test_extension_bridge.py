import json
import threading
from contextlib import contextmanager

import httpx
import pytest

from bili_platform.extension_bridge import ExtensionBridge, page_payload
from bili_platform.library import Library
from bili_platform.web import make_server

ORIGIN = "chrome-extension://" + "a" * 32
OTHER = "chrome-extension://" + "b" * 32


@contextmanager
def serving(library):
    server = make_server(library, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as web:
            yield server, web
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def bootstrap(web):
    assert web.get("/connection").status_code == 200
    return {"X-Library-Token": web.get("/api/extension/state").json()["token"]}


def message(mid="test-message-0001", text="你好"):
    return {"id": mid, "payload": {"type": "text", "data": text}}


def pair(bridge):
    return bridge.pair(ORIGIN, bridge.pairing_code()["code"])["token"]


def test_real_http_pair_two_directions_and_original_library_unchanged(tmp_path):
    lib = Library(tmp_path)
    before = lib.state()
    with serving(lib) as (server, web):
        assert web.get("/api/extension/state").status_code == 403
        headers = bootstrap(web)
        code = web.post("/api/extension/code", json={}, headers=headers).json()["result"]["code"]
        ext = {"Origin": ORIGIN}
        preflight = web.options("/bridge/pair", headers=ext)
        assert preflight.status_code == 204
        assert preflight.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert "Access-Control-Allow-Credentials" not in preflight.headers
        response = web.post("/bridge/pair", json={"code": code}, headers=ext)
        assert response.status_code == 200
        token = response.json()["token"]
        ext["Authorization"] = "Bearer " + token
        assert web.post("/bridge/message", json=message(), headers=ext).status_code == 200
        sent = message("workbench-message-0001", "收到，来自工作台")
        assert web.post("/api/extension/send", json=sent, headers=headers).status_code == 200
        # 未回执前重投，避免关闭面板导致丢失。
        for _ in range(2):
            polled = web.post("/bridge/poll", json={"acks": []}, headers=ext).json()
            assert polled["messages"][0]["id"] == sent["id"]
        assert web.post("/bridge/poll", json={"acks": [sent["id"]]}, headers=ext).json()["messages"] == []
        state = web.get("/api/extension/state").json()
        assert state["online"] and state["paired"]
        assert len(state["messages"]) == 2 and all(m["received"] for m in state["messages"])
        assert token not in json.dumps(state)
        assert server.library_sync.cookie_file is None
        assert web.get("/").status_code == 200 and web.get("/api/state").status_code == 200
    assert lib.state() == before


def test_page_identity_and_replay_are_validated(tmp_path):
    bridge = ExtensionBridge(Library(tmp_path))
    token = pair(bridge)
    value = {"id": "page-message-0001", "payload": {"type": "page", "data": {
        "url": "https://www.bilibili.com/video/BV1AbCdEfGhJ/?spm_id=private#tracking",
        "title": "当前视频", "bvid": "BV1AbCdEfGhJ"}}}
    for _ in range(2):
        bridge.extension_request(ORIGIN, token, "message", value)
    rows = bridge.state()["messages"]
    assert len(rows) == 1
    assert rows[0]["payload"]["data"]["url"] == "https://www.bilibili.com/video/BV1AbCdEfGhJ/"
    value["payload"]["data"]["title"] = "换了内容"
    with pytest.raises(ValueError, match="重复"):
        bridge.extension_request(ORIGIN, token, "message", value)
    value["payload"]["data"]["bvid"] = "BV1abcdefghj"
    with pytest.raises(ValueError, match="BVID"):
        bridge.extension_request(ORIGIN, token, "message", value)


@pytest.mark.parametrize("url", ["http://www.bilibili.com/video/BV1AbCdEfGhJ", "https://www.bilibili.com.evil/video/BV1AbCdEfGhJ", "https://user@www.bilibili.com/video/BV1AbCdEfGhJ", "https://127.0.0.1/video/BV1AbCdEfGhJ", "https://www.bilibili.com:1234/video/BV1AbCdEfGhJ", "https://www.bilibili.com/"])
def test_arbitrary_urls_and_credentials_rejected(url):
    with pytest.raises(ValueError):
        page_payload({"url": url, "title": "页面", "bvid": "BV1AbCdEfGhJ"})


def test_credentials_survive_restart_but_revocation_and_origin_binding_hold(tmp_path):
    bridge = ExtensionBridge(Library(tmp_path))
    token = pair(bridge)
    bridge.send(message("pending-message-01"))
    new = ExtensionBridge(Library(tmp_path))
    assert not new.state()["online"] and new.state()["paired"]
    assert new.extension_request(ORIGIN, token, "poll", {})["messages"][0]["id"] == "pending-message-01"
    with pytest.raises(PermissionError):
        new.extension_request(OTHER, token, "poll", {})
    new.revoke()
    after = ExtensionBridge(Library(tmp_path))
    with pytest.raises(PermissionError, match="失效"):
        after.extension_request(ORIGIN, token, "poll", {})


def test_pair_wrong_expired_single_use_and_heartbeat(tmp_path):
    clock = [100.0]
    bridge = ExtensionBridge(Library(tmp_path), clock=lambda: clock[0])
    code = bridge.pairing_code()["code"]
    with pytest.raises(PermissionError):
        bridge.pair(ORIGIN, "WRONG")
    token = bridge.pair(ORIGIN, code)["token"]
    with pytest.raises(PermissionError):
        bridge.pair(ORIGIN, code)
    assert bridge.state()["online"]
    clock[0] += 9
    assert not bridge.state()["online"]
    bridge.extension_request(ORIGIN, token, "poll", {})
    assert bridge.state()["online"]
    code = bridge.pairing_code()["code"]
    clock[0] += 601
    with pytest.raises(PermissionError, match="过期"):
        bridge.pair(ORIGIN, code)


def test_no_browser_commands_or_workbench_access_via_extension(tmp_path):
    with serving(Library(tmp_path)) as (server, web):
        header = bootstrap(web)
        token = pair(server.extension_bridge)
        ext = {"Origin": ORIGIN, "Authorization": "Bearer " + token}
        for path in ("/api/state", "/api/extension/state"):
            assert web.get(path, headers=ext).status_code == 403
        assert web.post("/api/extension/revoke", json={}, headers=ext).status_code == 403
        for origin in ("https://www.bilibili.com", "https://evil.invalid", "null", ""):
            r = web.post("/bridge/poll", json={}, headers={"Origin": origin})
            assert r.status_code == 403 and "Access-Control-Allow-Origin" not in r.headers
        assert web.post("/bridge/poll", json={}, headers={**ext, "Host": "evil.invalid"}).status_code == 403
        assert web.options("/api/state", headers=ext).status_code == 403
        assert web.post("/bridge/execute", json={}, headers=ext).status_code == 400
        assert web.post("/bridge/message", json={"id": "bad-command-01", "payload": {"type": "execute", "data": "danger"}}, headers=ext).status_code == 400
        assert web.post("/bridge/message", json={"payload": {}, "id": "test-secret-01", "cookie": "never"}, headers=ext).status_code == 400
        assert web.post("/bridge/poll", content="broken", headers={**ext, "Content-Type":"application/json"}).status_code == 400
        assert web.post("/bridge/poll", json={"x": "z"*9000}, headers=ext).status_code == 400
        assert web.post("/api/extension/code", json={}, headers={**header, "Origin":"https://evil.invalid"}).status_code == 403


def test_new_pair_invalidates_old_and_queued_text_survives_http_restart(tmp_path):
    lib = Library(tmp_path)
    with serving(lib) as (server, web):
        token = pair(server.extension_bridge)
        token2 = pair(server.extension_bridge)
        with pytest.raises(PermissionError):
            server.extension_bridge.extension_request(ORIGIN, token, "poll", {})
        headers = bootstrap(web)
        assert web.post("/api/extension/send", json=message(), headers=headers).status_code == 200
    with serving(Library(tmp_path)) as (_, web):
        data = web.post("/bridge/poll", json={}, headers={"Origin":ORIGIN,"Authorization":"Bearer "+token2}).json()
        assert data["messages"][0]["payload"]["data"] == "你好"


def test_full_queue_allows_idempotent_retry_but_not_new_message(tmp_path):
    bridge = ExtensionBridge(Library(tmp_path))
    token = pair(bridge)
    for index in range(50):
        bridge.send(message(f"queued-message-{index:04d}"))
    bridge.send(message("queued-message-0049"))
    with pytest.raises(ValueError, match="50条"):
        bridge.send(message("queued-message-0050"))
    polled = bridge.extension_request(ORIGIN, token, "poll", {})
    assert len(polled["messages"]) == 50
    bridge.extension_request(ORIGIN, token, "poll", {"acks":[polled["messages"][0]["id"]]})
    bridge.send(message("queued-message-0050"))
