import copy

import pytest

from bili_platform.operations import make_plan
from bili_platform.protocol import PlatformError, fingerprint
from bili_platform.session_review import accept_session, prepare_session
from bili_platform.workspace import Workspace


def dataset():
    value = {
        "schema": 1,
        "account_mid": 42,
        "source_folder": {"id": 1, "title": "示例来源"},
        "inventory_complete": False,
        "videos": [
            {
                "bvid": "BVfixture",
                "aid": 7,
                "title": "测试",
                "all_parts_verified": True,
                "parts": [{"cid": 9, "segments": [{"text": "努力就会成功", "start": 1, "end": 2}]}],
            },
            {
                "bvid": "BVblocked",
                "aid": 8,
                "title": "异常",
                "parts": [],
                "all_parts_verified": False,
            },
        ],
    }
    value["snapshot_hash"] = fingerprint(value)
    return value


def decision(packet):
    return {
        "packet_hash": packet["packet_hash"],
        "results": [
            {
                "bvid": "BVfixture",
                "result": {
                    "label": "鸡汤",
                    "content_types": ["泛化激励"],
                    "formats": ["演讲"],
                    "topics": [],
                    "confidence": 0.95,
                    "rationale": "固定测试数据，不代表真实分类质量",
                    "evidence": [{"segment_id": "9:0", "quote": "努力就会成功"}],
                },
            }
        ],
    }


def test_session_decision_preserves_evidence_and_uses_existing_move_flow(tmp_path):
    data = dataset()
    packet = prepare_session(data, session_ref="codex://fixture")
    assert len(packet["items"]) == 1
    ws = Workspace(tmp_path)
    results = accept_session(data, packet, decision(packet), ws)
    assert results["usage"]["actual_tokens"] is None
    assert results["results"][0]["evidence"][0]["start"] == 1
    assert not results["results"][1]["move_eligible"]
    assert len(make_plan(data, results)["items"]) == 1
    accept_session(data, packet, decision(packet), ws)
    with ws.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM classification").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["quote", "omitted", "duplicate", "scope", "hash"])
def test_bad_session_results_are_rejected_before_saving(tmp_path, change):
    data = dataset()
    packet = prepare_session(data, session_ref="codex://fixture")
    value = decision(packet)
    if change == "quote":
        value["results"][0]["result"]["evidence"][0]["quote"] = "没有出现的字幕"
    elif change == "omitted":
        value["results"] = []
    elif change == "duplicate":
        value["results"].append(copy.deepcopy(value["results"][0]))
    elif change == "scope":
        value["results"][0]["bvid"] = "BVblocked"
    else:
        value["packet_hash"] = "changed"
    ws = Workspace(tmp_path)
    with pytest.raises(PlatformError):
        accept_session(data, packet, value, ws)
    with ws.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM classification").fetchone()[0] == 0


def test_session_packet_tampering_and_unverified_scope_are_blocked(tmp_path):
    data = dataset()
    with pytest.raises(PlatformError):
        prepare_session(data, session_ref="codex://fixture", bvids=["BVblocked"])
    with pytest.raises(PlatformError):
        prepare_session(data, session_ref="codex://fixture", bvids=["BVnotinlist"])
    packet = prepare_session(data, session_ref="codex://fixture")
    value = decision(packet)
    packet["items"][0]["segments"][0]["text"] = "篡改证据"
    packet["packet_hash"] = fingerprint({k: v for k, v in packet.items() if k != "packet_hash"})
    value["packet_hash"] = packet["packet_hash"]
    with pytest.raises(PlatformError):
        accept_session(data, packet, value, Workspace(tmp_path))


def test_other_session_reuses_same_content_without_review(tmp_path):
    data = dataset()
    ws = Workspace(tmp_path)
    first = prepare_session(data, session_ref="codex://first", workspace=ws)
    accept_session(data, first, decision(first), ws)
    second = prepare_session(data, session_ref="codex://second", workspace=ws)
    assert second["items"] == [] and len(second["reused"]) == 1
    assert second["input_upper_estimate"] == 0
    out = accept_session(data, second, {"packet_hash": second["packet_hash"], "results": []}, ws)
    assert out["results"][0]["cached"]
    assert out["results"][0]["source_session_ref"] == "codex://first"
    assert len(make_plan(data, out)["items"]) == 1


def test_changed_content_or_rules_requires_new_review(tmp_path, monkeypatch):
    import bili_platform.session_review as module

    data = dataset()
    ws = Workspace(tmp_path)
    first = prepare_session(data, session_ref="codex://first", workspace=ws)
    accept_session(data, first, decision(first), ws)
    changed = copy.deepcopy(data)
    changed["videos"][0]["parts"][0]["segments"][0]["text"] = "原来的判断已不适用"
    changed["snapshot_hash"] = fingerprint(
        {k: v for k, v in changed.items() if k != "snapshot_hash"}
    )
    assert len(prepare_session(changed, session_ref="codex://new", workspace=ws)["items"]) == 1
    monkeypatch.setattr(module, "SYSTEM", module.SYSTEM + "规则发生变化")
    assert len(prepare_session(data, session_ref="codex://new", workspace=ws)["items"]) == 1


def test_changed_cache_after_packet_is_not_silently_accepted(tmp_path):
    data = dataset()
    ws = Workspace(tmp_path)
    first = prepare_session(data, session_ref="codex://first", workspace=ws)
    accept_session(data, first, decision(first), ws)
    second = prepare_session(data, session_ref="codex://second", workspace=ws)
    key = second["reused"][0]["cache_key"]
    value = ws.cached(key)
    value["rationale"] = "缓存被其他任务更新"
    ws.put(key, value)
    with pytest.raises(PlatformError):
        accept_session(data, second, {"packet_hash": second["packet_hash"], "results": []}, ws)


def test_pending_review_rejects_same_length_instruction_change(tmp_path, monkeypatch):
    import bili_platform.session_review as module

    data = dataset()
    p = prepare_session(data, session_ref="codex://first")
    value = decision(p)
    monkeypatch.setattr(module, "SYSTEM", module.SYSTEM.replace("鸡汤", "空话"))
    with pytest.raises(PlatformError):
        accept_session(data, p, value, Workspace(tmp_path))
