"""当前 Codex 会话审阅有界证据，按内容和规则复用，不调用模型 API。"""

from .classification import (
    RULE_VERSION,
    SYSTEM,
    evidence_for,
    messages_for,
    token_upper,
    validate_result,
)
from .operations import verify_snapshot
from .protocol import PlatformError, fingerprint


def cache_key(evidence):
    return fingerprint(
        {
            "mode": "codex_session",
            "rule": RULE_VERSION,
            "instructions": SYSTEM,
            "evidence": evidence,
        }
    )


def _raw(value):
    return {
        k: (
            [{"segment_id": q["segment_id"], "quote": q["quote"]} for q in value["evidence"]]
            if k == "evidence"
            else value[k]
        )
        for k in (
            "label",
            "content_types",
            "formats",
            "topics",
            "confidence",
            "rationale",
            "evidence",
        )
    }


def prepare_session(dataset, *, session_ref, bvids=(), max_chars=6000, workspace=None):
    verify_snapshot(dataset)
    if not session_ref or max_chars < 0:
        raise PlatformError("configuration", "需要真实会话出处和有效文本范围")
    selected = set(bvids)
    videos = {v.get("bvid"): v for v in dataset["videos"]}
    if selected - videos.keys():
        raise PlatformError("identity", "审阅目标不属于当前工作集")
    items, reused = [], []
    for video in dataset["videos"]:
        if selected and video.get("bvid") not in selected:
            continue
        evidence = evidence_for(video, max_chars)
        if not evidence["all_parts_verified"] or not evidence["segments"]:
            if selected:
                raise PlatformError("identity", "指定视频没有完整核对的字幕证据")
            continue
        key = cache_key(evidence)
        cached = workspace.cached(key) if workspace else None
        if cached:
            validate_result(_raw(cached), evidence)
            reused.append(
                {"bvid": evidence["bvid"], "cache_key": key, "value_hash": fingerprint(cached)}
            )
        else:
            items.append(evidence)
    packet = {
        "schema": 2,
        "mode": "codex_session",
        "session_ref": session_ref,
        "snapshot_hash": dataset["snapshot_hash"],
        "rule_version": RULE_VERSION,
        "instructions_hash": fingerprint(SYSTEM),
        "max_chars": max_chars,
        "selected_bvids": sorted(selected),
        "items": items,
        "reused": reused,
        "input_upper_estimate": sum(token_upper(messages_for(e)) for e in items),
        "usage_note": "仅为待审文本估计；订阅会话额度以 Codex 为准，不是 API 计费记录",
    }
    packet["packet_hash"] = fingerprint(packet)
    return packet


def accept_session(dataset, packet, decisions, workspace):
    """先核对完整范围和缓存快照，再写入成功结果。"""
    full = prepare_session(
        dataset,
        session_ref=packet.get("session_ref"),
        bvids=packet.get("selected_bvids", ()),
        max_chars=packet.get("max_chars", 6000),
    )
    evidence_by_id = {e["bvid"]: e for e in full["items"]}
    reused = packet.get("reused", [])
    if not isinstance(reused, list):
        raise PlatformError("identity", "缓存引用结构无效")
    cached_values = {}
    for reference in reused:
        if not isinstance(reference, dict) or set(reference) != {"bvid", "cache_key", "value_hash"}:
            raise PlatformError("identity", "缓存引用字段无效")
        bvid = reference["bvid"]
        evidence = evidence_by_id.get(bvid)
        if bvid in cached_values or not evidence or reference["cache_key"] != cache_key(evidence):
            raise PlatformError("identity", "缓存引用重复或不属于当前内容")
        cached = workspace.cached(reference["cache_key"])
        if not cached or fingerprint(cached) != reference["value_hash"]:
            raise PlatformError("identity", "缓存已变化，请重新准备审阅")
        validated = validate_result(_raw(cached), evidence)
        cached_values[bvid] = {**cached, **validated, "cached": True}
    full["items"] = [e for e in full["items"] if e["bvid"] not in cached_values]
    full["reused"] = reused
    full["input_upper_estimate"] = sum(token_upper(messages_for(e)) for e in full["items"])
    full["packet_hash"] = fingerprint({k: v for k, v in full.items() if k != "packet_hash"})
    if packet != full:
        raise PlatformError("identity", "审阅包与工作集、范围或规则不一致，请重新准备")
    if not isinstance(decisions, dict) or set(decisions) != {"packet_hash", "results"}:
        raise PlatformError("classification", "会话结果结构无效")
    if decisions["packet_hash"] != packet["packet_hash"]:
        raise PlatformError("identity", "会话结果不属于本次审阅包")
    rows = decisions["results"]
    if not isinstance(rows, list) or not all(
        isinstance(r, dict) and set(r) == {"bvid", "result"} and isinstance(r["bvid"], str)
        for r in rows
    ):
        raise PlatformError("classification", "会话分类条目无效")
    by_id = {r["bvid"]: r["result"] for r in rows}
    if len(by_id) != len(rows) or set(by_id) != {e["bvid"] for e in packet["items"]}:
        raise PlatformError("classification", "会话结果遗漏、重复或越过审阅范围")
    provider = {
        "kind": "codex_session",
        "session_ref": packet["session_ref"],
        "model": "current_session_unreported",
    }
    videos = {v["bvid"]: v for v in dataset["videos"]}
    values = dict(cached_values)
    for evidence in packet["items"]:
        bvid = evidence["bvid"]
        value = validate_result(by_id[bvid], evidence)
        value.update(
            bvid=bvid,
            aid=videos[bvid]["aid"],
            resource_type=videos[bvid].get("type", 2),
            title=videos[bvid]["title"],
            cache_key=cache_key(evidence),
            evidence_hash=fingerprint(evidence),
            source_session_ref=packet["session_ref"],
            status="classified",
        )
        values[bvid] = value
    results = []
    for video in dataset["videos"]:
        bvid = video["bvid"]
        results.append(
            values.get(bvid)
            or {
                "bvid": bvid,
                "label": "证据不足",
                "move_eligible": False,
                "rationale": video.get("evidence_error_detail")
                or (
                    "本轮没有取得可用字幕"
                    if not video.get("parts")
                    else "未进入本轮已核对字幕审阅范围"
                ),
                "evidence": [],
                "status": "not_reviewed",
            }
        )
    for bvid, value in values.items():
        if bvid not in cached_values:
            workspace.put(value["cache_key"], value)
    return {
        "schema": 1,
        "snapshot_hash": dataset["snapshot_hash"],
        "rule_version": RULE_VERSION,
        "provider": provider,
        "packet_hash": packet["packet_hash"],
        "results": results,
        "usage": {
            "billing": "codex_session",
            "actual_tokens": None,
            "input_upper_estimate": packet["input_upper_estimate"],
            "reviewed": len(packet["items"]),
            "reused": len(reused),
        },
    }
