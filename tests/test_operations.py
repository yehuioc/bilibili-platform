import copy

import pytest

from bili_platform.operations import apply_plan, make_plan, make_probe_plan
from bili_platform.protocol import PlatformError, fingerprint
from bili_platform.workspace import Workspace


class PlatformFixture:
    def __init__(self):
        self.identity = {"mid": 42}
        self.items = {1: {7}}
        self.writes = []
        self.timeout_after_move = False

    def nav(self):
        return self.identity

    def folders(self):
        return [{"id": 1, "title": "示例来源"}] + (
            [{"id": 2, "title": "鸡汤"}] if 2 in self.items else []
        )

    def inventory(self, folder):
        return [{"id": i, "type": 2} for i in self.items[folder["id"]]], True

    def create_folder(self, title):
        self.writes.append(("create", title))
        self.items[2] = set()
        return {"id": 2}

    def move(self, src, dst, aid, resource_type):
        self.writes.append(("move", src, dst, aid))
        self.items[src].remove(aid)
        self.items[dst].add(aid)
        if self.timeout_after_move:
            self.timeout_after_move = False
            raise PlatformError("write_uncertain", "fixture timeout after server applied")


def plan():
    data = {
        "schema": 1,
        "account_mid": 42,
        "source_folder": {"id": 1, "title": "示例来源"},
        "inventory_complete": False,
        "videos": [
            {
                "bvid": "BVfixture",
                "aid": 7,
                "type": 2,
                "title": "fixture",
                "all_parts_verified": True,
            }
        ],
    }
    data["snapshot_hash"] = fingerprint(data)
    results = {
        "snapshot_hash": data["snapshot_hash"],
        "results": [
            {
                "bvid": "BVfixture",
                "aid": 7,
                "label": "鸡汤",
                "coverage": "full",
                "confidence": 0.95,
                "evidence": [{"quote": "fixture"}],
                "move_eligible": True,
            }
        ],
    }
    return make_plan(data, results)


def test_create_move_readback_repeat_and_restore(tmp_path):
    platform = PlatformFixture()
    workspace = Workspace(tmp_path)
    p = plan()
    applied = apply_plan(platform, workspace, p, p["plan_hash"])
    assert applied["status"] == "complete" and platform.items == {1: set(), 2: {7}}
    repeat = apply_plan(platform, workspace, p, p["plan_hash"])
    assert repeat["status"] == "complete" and len(platform.writes) == 2
    restored = apply_plan(platform, workspace, p, p["plan_hash"], restore=True)
    assert restored["status"] == "complete" and platform.items == {1: {7}, 2: set()}
    apply_plan(platform, workspace, p, p["plan_hash"], restore=True)
    assert len(platform.writes) == 3


def test_timeout_checks_relation_without_retry(tmp_path):
    platform = PlatformFixture()
    platform.timeout_after_move = True
    p = plan()
    out = apply_plan(platform, Workspace(tmp_path), p, p["plan_hash"])
    assert out["status"] == "complete" and len(platform.writes) == 2


def test_wrong_account_or_tampered_plan_has_no_writes(tmp_path):
    platform = PlatformFixture()
    platform.identity = {"mid": 99}
    p = plan()
    with pytest.raises(PlatformError):
        apply_plan(platform, Workspace(tmp_path), p, p["plan_hash"])
    assert not platform.writes
    platform.identity = {"mid": 42}
    p["target_title"] = "other"
    with pytest.raises(PlatformError):
        apply_plan(platform, Workspace(tmp_path), p, p["plan_hash"])
    assert not platform.writes


def test_existing_target_membership_is_not_destroyed_on_restore(tmp_path):
    platform = PlatformFixture()
    platform.items[2] = {7}
    p = plan()
    ws = Workspace(tmp_path)
    out = apply_plan(platform, ws, p, p["plan_hash"])
    assert out["status"] == "partial" and not platform.writes
    out = apply_plan(platform, ws, p, p["plan_hash"], restore=True)
    assert out["status"] == "partial" and platform.items == {1: {7}, 2: {7}}


def test_offline_or_sampled_classification_cannot_generate_move():
    p = plan()
    bad = copy.deepcopy(p)
    bad["account_mid"] = None
    bad.pop("plan_hash")
    bad["snapshot_hash"] = fingerprint(bad)
    with pytest.raises(PlatformError):
        make_plan(bad, {"snapshot_hash": bad["snapshot_hash"], "results": []})


def test_explicit_operation_probe_roundtrip_needs_no_fake_classification(tmp_path):
    platform = PlatformFixture()
    data = {
        "account_mid": 42,
        "source_folder": {"id": 1, "title": "示例来源"},
        "videos": [{"bvid": "BVfixture", "aid": 7, "type": 2, "title": "实用内容"}],
    }
    data["snapshot_hash"] = fingerprint(data)
    p = make_probe_plan(data, "BVfixture")
    assert p["mode"] == "roundtrip_probe" and p["items"][0]["evidence"] == []
    ws = Workspace(tmp_path)
    assert apply_plan(platform, ws, p, p["plan_hash"])["status"] == "complete"
    assert apply_plan(platform, ws, p, p["plan_hash"], restore=True)["status"] == "complete"
    assert platform.items == {1: {7}, 2: set()}
    with pytest.raises(PlatformError):
        make_probe_plan(data, "BVnotinbatch")
