"""收藏写操作：预览、账号绑定、持久日志、读回与恢复。"""

from __future__ import annotations

from .protocol import PlatformError, fingerprint


def verify_snapshot(data):
    if data.get("snapshot_hash") != fingerprint(
        {k: v for k, v in data.items() if k != "snapshot_hash"}
    ):
        raise PlatformError("identity", "工作集发生变化，请重新生成预算与分类")


def make_plan(dataset, results, target="鸡汤"):
    verify_snapshot(dataset)
    if results.get("snapshot_hash") != dataset["snapshot_hash"]:
        raise PlatformError("identity", "分类结果不属于该工作集")
    if not dataset.get("account_mid"):
        raise PlatformError("identity", "离线资料未核对实时账号与收藏关系，不能生成执行计划")
    videos = {v.get("bvid"): v for v in dataset["videos"]}
    items, seen = [], set()
    for result in results["results"]:
        if not result.get("move_eligible"):
            continue
        video = videos.get(result.get("bvid"))
        if (
            not video
            or not video.get("all_parts_verified")
            or result.get("label") != "鸡汤"
            or result.get("coverage") != "full"
            or result.get("confidence", 0) < 0.9
            or not result.get("evidence")
            or result.get("aid") != video.get("aid")
        ):
            raise PlatformError("classification", "移动候选未通过完整证据检查")
        resource = (int(video["aid"]), int(video.get("type", 2)))
        if resource in seen:
            raise PlatformError("identity", "移动计划存在重复资源")
        seen.add(resource)
        items.append(
            {
                "bvid": video["bvid"],
                "aid": resource[0],
                "type": resource[1],
                "title": video["title"],
                "evidence": result["evidence"],
            }
        )
    plan = {
        "schema": 1,
        "account_mid": dataset["account_mid"],
        "source_folder": dataset["source_folder"],
        "target_title": target,
        "snapshot_hash": dataset["snapshot_hash"],
        "mode": "move",
        "items": items,
    }
    plan["plan_hash"] = fingerprint(plan)
    return plan


def make_probe_plan(dataset, bvid, target="鸡汤"):
    """显式授权的单条往返操作测试，不伪装成内容分类结果。"""
    verify_snapshot(dataset)
    if not dataset.get("account_mid"):
        raise PlatformError("identity", "操作测试需要已核对的实时账号清单")
    rows = [v for v in dataset["videos"] if v.get("bvid") == bvid and v.get("type", 2) == 2]
    if len(rows) != 1:
        raise PlatformError("identity", "操作测试条目未唯一匹配当前工作集")
    video = rows[0]
    plan = {
        "schema": 1,
        "account_mid": dataset["account_mid"],
        "source_folder": dataset["source_folder"],
        "target_title": target,
        "snapshot_hash": dataset["snapshot_hash"],
        "mode": "roundtrip_probe",
        "items": [
            {
                "bvid": bvid,
                "aid": int(video["aid"]),
                "type": 2,
                "title": video["title"],
                "evidence": [],
            }
        ],
    }
    plan["plan_hash"] = fingerprint(plan)
    return plan


def _resources(client, folder):
    rows, complete = client.inventory(folder)
    if not complete:
        raise PlatformError("pagination", "关系核对未读完整，不能执行")
    return {(int(r.get("id") or r["aid"]), int(r.get("type", 2))) for r in rows}


def apply_plan(client, workspace, plan, confirmation, *, restore=False):
    digest = fingerprint({k: v for k, v in plan.items() if k != "plan_hash"})
    if confirmation != digest or plan.get("plan_hash") != digest:
        raise PlatformError("authorization", "确认值与计划不一致")
    if plan.get("mode") not in ("move", "roundtrip_probe") or (
        plan["mode"] == "roundtrip_probe" and len(plan["items"]) != 1
    ):
        raise PlatformError("authorization", "操作类型或往返测试范围无效")
    if int(client.nav()["mid"]) != int(plan["account_mid"]):
        raise PlatformError("identity", "当前账号与移动计划不一致")
    if not plan["items"]:
        return {"status": "no_changes", "items": []}
    folders = client.folders()
    source = next((r for r in folders if int(r["id"]) == int(plan["source_folder"]["id"])), None)
    if not source or source["title"] != plan["source_folder"]["title"]:
        raise PlatformError("identity", "源收藏夹身份或名称变化")
    targets = [r for r in folders if r["title"] == plan["target_title"]]
    if len(targets) > 1:
        raise PlatformError("identity", "目标收藏夹重名，必须明确 ID")
    if not targets:
        if restore:
            raise PlatformError("identity", "目标收藏夹已不存在，不能自动恢复")
        try:
            client.create_folder(plan["target_title"])
        except PlatformError as exc:
            if exc.kind != "write_uncertain":
                raise
        # 即使创建响应成功也要确认实际归属；不盲目重试创建。
        targets = [r for r in client.folders() if r["title"] == plan["target_title"]]
        if len(targets) != 1:
            raise PlatformError("write_uncertain", "目标创建结果未确认，停止")
    target = targets[0]
    if int(source["id"]) == int(target["id"]):
        raise PlatformError("identity", "源与目标是同一收藏夹")
    source_set, target_set = _resources(client, source), _resources(client, target)
    decisions, submitted = [], []
    from_id, to_id = (target["id"], source["id"]) if restore else (source["id"], target["id"])
    from_set, to_set = (target_set, source_set) if restore else (source_set, target_set)
    for item in plan["items"]:
        key = (int(item["aid"]), int(item["type"]))
        operation_id = fingerprint({"plan": digest, "aid": key, "target": target["id"]})
        old = workspace.operation(operation_id)
        if restore and not old:
            decisions.append({"aid": key[0], "status": "no_owned_operation"})
            continue
        if key not in from_set and key in to_set and old:
            old["status"] = "restored" if restore else "verified"
            workspace.operation(operation_id, old)
            decisions.append({"aid": key[0], "status": old["status"]})
            continue
        if key not in from_set or key in to_set:
            decisions.append({"aid": key[0], "status": "relation_conflict"})
            continue
        record = {
            "status": "restore_submitted" if restore else "submitted",
            "item": item,
            "source_id": source["id"],
            "target_id": target["id"],
        }
        workspace.operation(operation_id, record)
        submitted.append((operation_id, record, key))
        try:
            client.move(from_id, to_id, key[0], key[1])
        except PlatformError as exc:
            record["error_kind"] = exc.kind
            workspace.operation(operation_id, record)
            # 不继续发送写请求；下面尝试只读核对已提交项。
            break
    if submitted:
        try:
            from_set = _resources(client, target if restore else source)
            to_set = _resources(client, source if restore else target)
        except PlatformError:
            for _, record, key in submitted:
                decisions.append(
                    {"aid": key[0], "status": "unverified", "error_kind": record.get("error_kind")}
                )
        else:
            for operation_id, record, key in submitted:
                record["status"] = (
                    ("restored" if restore else "verified")
                    if (key not in from_set and key in to_set)
                    else "unverified"
                )
                workspace.operation(operation_id, record)
                decisions.append({"aid": key[0], "status": record["status"]})
    handled = {r["aid"] for r in decisions}
    decisions.extend(
        {"aid": r["aid"], "status": "not_attempted"}
        for r in plan["items"]
        if r["aid"] not in handled
    )
    ok = {"verified", "restored"}
    return {
        "status": "complete" if all(r["status"] in ok for r in decisions) else "partial",
        "source_id": source["id"],
        "target_id": target["id"],
        "items": decisions,
    }
