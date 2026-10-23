import copy
import json

import httpx
import pytest

from bili_platform.classification import (
    budget_preview,
    classify,
    evidence_for,
    report_html,
    validate_result,
)
from bili_platform.protocol import PlatformError, fingerprint
from bili_platform.workspace import Workspace


def dataset():
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
                "title": "<script>alert(1)</script>",
                "all_parts_verified": True,
                "parts": [{"cid": 9, "segments": [{"text": "努力就会成功", "start": 1, "end": 2}]}],
            }
        ],
    }
    data["snapshot_hash"] = fingerprint(data)
    return data


def result():
    return {
        "label": "鸡汤",
        "content_types": ["情绪激励"],
        "formats": ["演讲"],
        "topics": [],
        "confidence": 0.95,
        "rationale": "泛化激励",
        "evidence": [{"segment_id": "9:0", "quote": "努力就会成功"}],
    }


def test_evidence_must_be_exact_and_sent():
    evidence = evidence_for(dataset()["videos"][0])
    value = validate_result(result(), evidence)
    assert value["move_eligible"] and value["evidence"][0]["start"] == 1
    bad = result()
    bad["evidence"][0]["quote"] = "模型编造的原话"
    with pytest.raises(PlatformError):
        validate_result(bad, evidence)
    bad = result()
    bad["evidence"][0]["segment_id"] = "99:0"
    with pytest.raises(PlatformError):
        validate_result(bad, evidence)


def test_partial_or_unknown_parts_never_auto_move():
    video = dataset()["videos"][0]
    video["parts"][0]["segments"] += [{"text": "具体方法和工具" * 100, "start": 3, "end": 9}]
    evidence = evidence_for(video, max_chars=10)
    assert evidence["coverage"] == "sampled"
    assert not validate_result(result(), evidence)["move_eligible"]
    video["all_parts_verified"] = False
    assert not validate_result(result(), evidence_for(video, max_chars=0))["move_eligible"]


def test_budget_and_cache_prevent_repeat_calls(tmp_path):
    data = dataset()
    ws = Workspace(tmp_path)
    calls = []
    provider = {"base_url": "https://model.fixture/v1", "model": "fixture", "api_key": "SECRET"}

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "fixture",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(result())}}
                ],
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    preview = budget_preview(data, provider)
    assert "SECRET" not in json.dumps(preview)
    with pytest.raises(PlatformError):
        classify(data, ws, provider, http=http)
    with pytest.raises(PlatformError) as caught:
        classify(data, ws, provider, allow_live=True, input_cap=1, output_cap=600, http=http)
    assert caught.value.kind == "budget" and not calls
    out = classify(
        data,
        ws,
        provider,
        allow_live=True,
        input_cap=preview["input_upper"],
        output_cap=600,
        http=http,
    )
    assert out["results"][0]["move_eligible"]
    again = classify(data, ws, provider, allow_live=True, input_cap=1, output_cap=1, http=http)
    assert again["results"][0]["cached"] and len(calls) == 1
    page = tmp_path / "preview.html"
    report_html(data, out, page)
    text = page.read_text(encoding="utf-8")
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_truncated_provider_output_does_not_cache(tmp_path):
    data = dataset()
    ws = Workspace(tmp_path)
    provider = {"base_url": "https://model.fixture/v1", "model": "fixture", "api_key": "fixture"}
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "model": "fixture",
                    "choices": [
                        {"finish_reason": "length", "message": {"content": json.dumps(result())}}
                    ],
                },
            )
        )
    )
    with pytest.raises(PlatformError) as caught:
        classify(data, ws, provider, allow_live=True, input_cap=100000, output_cap=600, http=http)
    assert caught.value.kind == "classification"
    with ws.connect() as c:
        assert c.execute("SELECT count(*) FROM classification").fetchone()[0] == 0


def test_rule_change_or_content_change_does_not_reuse_cache(tmp_path):
    ws = Workspace(tmp_path)
    ws.put("old", {"label": "鸡汤"})
    changed = copy.deepcopy(dataset())
    changed["videos"][0]["parts"][0]["segments"][0]["text"] = "新内容"
    assert fingerprint(evidence_for(changed["videos"][0])) != fingerprint(
        evidence_for(dataset()["videos"][0])
    )


def test_preview_reuses_saved_results_without_model_call(tmp_path):
    from bili_platform.cli import main
    from bili_platform.workspace import save_json

    data = dataset()
    save_json(tmp_path / "dataset.json", data)
    value = validate_result(result(), evidence_for(data["videos"][0]))
    value["bvid"] = "BVfixture"
    save_json(
        tmp_path / "results.json", {"snapshot_hash": data["snapshot_hash"], "results": [value]}
    )
    assert (
        main(["--workspace", str(tmp_path), "preview", "--dataset", str(tmp_path / "dataset.json")])
        == 0
    )
    page = (tmp_path / "preview.html").read_text(encoding="utf-8")
    assert "鸡汤" in page and "尚未调用模型" not in page


@pytest.mark.parametrize(
    "payload", [[], None, {"usage": None}, {"model": "fixture", "choices": [None]}]
)
def test_malformed_provider_output_keeps_failure_bounded(tmp_path, payload):
    ws = Workspace(tmp_path)
    provider = {"base_url": "https://model.fixture/v1", "model": "fixture", "api_key": "fake"}
    http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=json.dumps(payload)))
    )
    with pytest.raises(PlatformError) as caught:
        classify(
            dataset(), ws, provider, allow_live=True, input_cap=100000, output_cap=600, http=http
        )
    assert caught.value.kind == "provider"
    with ws.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM classification").fetchone()[0] == 0
        assert c.execute("SELECT SUM(reserved_output) FROM usage").fetchone()[0] == 600
