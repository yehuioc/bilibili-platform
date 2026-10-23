"""沿收藏清单→字幕→分类→移动→恢复验证业务链；不访问真实服务。"""

import json

import httpx
import pytest

from bili_platform.classification import budget_preview, classify
from bili_platform.operations import apply_plan, make_plan
from bili_platform.workspace import Workspace, snapshot


class Source:
    def __init__(self, bad=None):
        self.identity = {"mid": 42}
        self.bad = bad
        self.relations = {1: {7}}
        self.writes = []

    def nav(self):
        return self.identity

    def folders(self):
        return [{"id": 1, "title": "示例来源"}] + (
            [{"id": 2, "title": "鸡汤"}] if 2 in self.relations else []
        )

    def resolve_folder(self, title):
        return next(f for f in self.folders() if f["title"] == title)

    def inventory(self, folder, **kwargs):
        return [
            {"id": aid, "bvid": "BVfixture", "title": "测试内容", "type": 2}
            for aid in self.relations[folder["id"]]
        ], True

    def video(self, bvid):
        return {
            "aid": 99 if self.bad == "aid" else 7,
            "pages": [{"cid": 9}, {"cid": 9 if self.bad == "duplicate" else 10}],
        }

    def transcript(self, bvid, cid, aid):
        return {
            "cid": cid,
            "identity_verified": True,
            "segments": [{"start": 1, "end": 2, "text": "努力就会成功"}],
        }

    def create_folder(self, title):
        self.relations[2] = set()
        self.writes.append("create")

    def move(self, src, dst, aid, resource_type):
        self.relations[src].remove(aid)
        self.relations[dst].add(aid)
        self.writes.append("move")


@pytest.mark.parametrize("bad", ["aid", "duplicate"])
def test_inconsistent_video_identity_never_becomes_verified(bad):
    data = snapshot(Source(bad), "示例来源", fetch_subtitles=True)
    assert not data["videos"][0]["all_parts_verified"]
    assert data["videos"][0]["evidence_error"] == "identity"


def test_saved_snapshot_to_cached_classification_move_and_restore(tmp_path):
    platform = Source()
    data = snapshot(platform, "示例来源", fetch_subtitles=True)
    assert data["videos"][0]["all_parts_verified"]
    ws = Workspace(tmp_path)
    provider = {"base_url": "https://fixture.test/v1", "model": "fixture", "api_key": "fake"}
    calls = []

    def model(request):
        calls.append(request)
        sent = json.loads(request.content)["messages"][1]["content"]
        assert "9:0" in sent and "10:0" in sent
        value = {
            "label": "鸡汤",
            "content_types": ["泛化激励"],
            "formats": ["演讲"],
            "topics": [],
            "confidence": 0.95,
            "rationale": "测试固定判断，仅验证流程",
            "evidence": [{"segment_id": "9:0", "quote": "努力就会成功"}],
        }
        return httpx.Response(
            200,
            json={
                "model": "fixture",
                "usage": {},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(model))
    preview = budget_preview(data, provider)
    results = classify(
        data,
        ws,
        provider,
        allow_live=True,
        input_cap=preview["input_upper"],
        output_cap=600,
        http=http,
    )
    cached = classify(data, ws, provider, allow_live=True, input_cap=1, output_cap=1, http=http)
    assert cached["results"][0]["cached"] and len(calls) == 1
    plan = make_plan(data, results)
    assert apply_plan(platform, ws, plan, plan["plan_hash"])["status"] == "complete"
    assert platform.relations == {1: set(), 2: {7}}
    assert apply_plan(platform, ws, plan, plan["plan_hash"], restore=True)["status"] == "complete"
    assert platform.relations == {1: {7}, 2: set()}
