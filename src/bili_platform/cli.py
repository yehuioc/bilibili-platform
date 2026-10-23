"""CLI 是公共底座的消费者，不维护平台协议。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .classification import budget_preview, classify, provider_from_file, report_html
from .client import BilibiliClient
from .operations import apply_plan, make_plan, make_probe_plan, verify_snapshot
from .plugins import discover
from .protocol import PlatformError, fingerprint
from .session_review import accept_session, prepare_session
from .workspace import Workspace, load_ingestion, read_json, save_json, snapshot


def main(argv=None):
    # 服务命令保持独立参数入口，复用同一包、平台客户端和私人数据边界。
    actual = list(sys.argv[1:] if argv is None else argv)
    if actual and actual[0] == "serve":
        from .web import main as serve
        return serve(actual[1:])
    if actual and actual[0] == "control":
        from .control import main as control
        return control(actual[1:])
    parser = argparse.ArgumentParser(description="B站公共底座与收藏内容分类")
    parser.add_argument("--workspace", default="private")
    parser.add_argument("--cookie-file", default=os.environ.get("BILIBILI_COOKIE_FILE"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plugins")
    folders_command = commands.add_parser("folders")
    folders_command.add_argument("--name", action="append")
    preview_command = commands.add_parser("preview")
    preview_command.add_argument("--dataset", type=Path, required=True)
    preview_command.add_argument("--results", type=Path)
    p = commands.add_parser("prepare-session")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--session-ref", required=True)
    p.add_argument("--bvid", action="append", default=[])
    p.add_argument("--max-chars", type=int, default=6000)
    p = commands.add_parser("accept-session")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--packet", type=Path, required=True)
    p.add_argument("--decisions", type=Path, required=True)
    diagnostic = commands.add_parser("diagnose-subtitle")
    diagnostic.add_argument("--bvid", required=True)
    diagnostic.add_argument("--signed", action="store_true")
    diagnostic.add_argument("--verify-text", action="store_true")
    for name in ("prepare-cache", "snapshot"):
        p = commands.add_parser(name)
        p.add_argument("--folder", default="待整理")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--ingestion-db", type=Path, required=True)
        p.add_argument("--corpus", type=Path, required=True)
        p.add_argument("--raw-root", type=Path, required=True)
        if name == "snapshot":
            p.add_argument("--fetch-subtitles", action="store_true")
    for name in ("budget", "classify"):
        p = commands.add_parser(name)
        p.add_argument("--dataset", type=Path, required=True)
        p.add_argument("--provider-config", type=Path, required=True)
        p.add_argument("--max-chars", type=int, default=6000)
        p.add_argument("--max-output", type=int, default=600)
        p.add_argument("--summary", action="store_true")
        if name == "classify":
            p.add_argument("--allow-live", action="store_true")
            p.add_argument("--input-cap", type=int, required=True)
            p.add_argument("--output-cap", type=int, required=True)
    p = commands.add_parser("plan")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--target", default="鸡汤")
    p = commands.add_parser("plan-probe")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--bvid", required=True)
    p.add_argument("--target", default="鸡汤")
    for name in ("apply", "restore"):
        p = commands.add_parser(name)
        p.add_argument("--plan", type=Path, required=True)
        p.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    workspace = Workspace(args.workspace)
    client = None
    try:
        output = None
        if args.command == "plugins":
            output = {
                k: {"api_version": v.api_version, "capabilities": v.capabilities}
                for k, v in discover().items()
            }
        elif args.command == "prepare-session":
            dataset = read_json(args.dataset)
            packet = prepare_session(
                dataset,
                session_ref=args.session_ref,
                bvids=args.bvid,
                max_chars=args.max_chars,
                workspace=workspace,
            )
            path = workspace.root / "session-packet.json"
            save_json(path, packet)
            output = {
                "packet": str(path),
                "packet_hash": packet["packet_hash"],
                "items": len(packet["items"]),
                "reused": len(packet["reused"]),
                "input_upper_estimate": packet["input_upper_estimate"],
            }
            if not packet["items"]:
                results = accept_session(
                    dataset,
                    packet,
                    {"packet_hash": packet["packet_hash"], "results": []},
                    workspace,
                )
                save_json(workspace.root / "results.json", results)
                report_html(dataset, results, workspace.root / "preview.html")
                output["results"] = str(workspace.root / "results.json")
        elif args.command == "accept-session":
            dataset = read_json(args.dataset)
            results = accept_session(
                dataset, read_json(args.packet), read_json(args.decisions), workspace
            )
            save_json(workspace.root / "results.json", results)
            report_html(dataset, results, workspace.root / "preview.html")
            output = {
                "results": str(workspace.root / "results.json"),
                "classified": sum(r["status"] == "classified" for r in results["results"]),
                "preview": str(workspace.root / "preview.html"),
            }
        elif args.command == "preview":
            dataset = read_json(args.dataset)
            verify_snapshot(dataset)
            result_path = args.results or workspace.root / "results.json"
            results = read_json(result_path) if args.results or result_path.is_file() else {}
            if results and results.get("snapshot_hash") != dataset["snapshot_hash"]:
                raise PlatformError("identity", "分类预览与工作集不匹配")
            path = workspace.root / "preview.html"
            report_html(dataset, results, path)
            output = {"preview": str(path)}
        elif args.command in ("prepare-cache", "snapshot"):
            if args.limit < 0:
                raise PlatformError("configuration", "limit 不能为负数；0 表示全量")
            cache = load_ingestion(args.ingestion_db, args.corpus, args.raw_root, args.folder)
            if args.command == "snapshot":
                if not args.cookie_file:
                    raise PlatformError("auth", "缺少 --cookie-file")
                client = BilibiliClient.from_cookie_file(args.cookie_file)
                dataset = snapshot(
                    client,
                    args.folder,
                    limit=args.limit,
                    cache=cache,
                    fetch_subtitles=args.fetch_subtitles,
                )
            else:
                videos = sorted(cache, key=lambda v: v["bvid"])
                if args.limit:
                    videos = videos[: args.limit]
                dataset = {
                    "schema": 1,
                    "account_mid": None,
                    "source_folder": {"id": None, "title": args.folder},
                    "inventory_complete": False,
                    "videos": videos,
                }
                dataset["snapshot_hash"] = fingerprint(dataset)
            path = workspace.root / "dataset.json"
            save_json(path, dataset)
            report_html(dataset, {}, workspace.root / "preview.html")
            output = {
                "dataset": str(path),
                "videos": len(dataset["videos"]),
                "complete": dataset["inventory_complete"],
                "live_account_verified": bool(dataset["account_mid"]),
                "preview": str(workspace.root / "preview.html"),
            }
        elif args.command in ("budget", "classify"):
            if args.max_chars < 0 or args.max_output <= 0:
                raise PlatformError("configuration", "文本与输出限制无效")
            dataset, provider = (
                read_json(args.dataset),
                provider_from_file(args.provider_config, require_key=args.command == "classify"),
            )
            verify_snapshot(dataset)
            if args.command == "budget":
                output = budget_preview(
                    dataset, provider, max_chars=args.max_chars, max_output=args.max_output
                )
                save_json(workspace.root / "budget.json", output)
                if args.summary:
                    output = {k: v for k, v in output.items() if k != "items"}
                    output["credential_ready"] = bool(provider["api_key"])
            else:
                results = classify(
                    dataset,
                    workspace,
                    provider,
                    allow_live=args.allow_live,
                    input_cap=args.input_cap,
                    output_cap=args.output_cap,
                    max_chars=args.max_chars,
                    max_output=args.max_output,
                )
                save_json(workspace.root / "results.json", results)
                report_html(dataset, results, workspace.root / "preview.html")
                output = {
                    "results": str(workspace.root / "results.json"),
                    "classified": len(results["results"]),
                    "preview": str(workspace.root / "preview.html"),
                }
        elif args.command in ("plan", "plan-probe"):
            plan = (
                make_probe_plan(read_json(args.dataset), args.bvid, args.target)
                if args.command == "plan-probe"
                else make_plan(read_json(args.dataset), read_json(args.results), args.target)
            )
            path = workspace.root / (
                "probe-plan.json" if args.command == "plan-probe" else "move-plan.json"
            )
            save_json(path, plan)
            output = {
                "plan": str(path),
                "hash": plan["plan_hash"],
                "moves": len(plan["items"]),
            }
        else:
            if not args.cookie_file:
                raise PlatformError("auth", "缺少 --cookie-file")
            client = BilibiliClient.from_cookie_file(args.cookie_file)
            if args.command == "diagnose-subtitle":
                from urllib.parse import urlsplit

                from .protocol import subtitle_url

                video = client.video(args.bvid)
                cid = video["pages"][0]["cid"]
                aid = video["aid"]
                params = {"bvid": args.bvid, "aid": aid, "cid": cid}
                data = client.request(
                    "/x/player/wbi/v2" if args.signed else "/x/player/v2",
                    client.signed(params) if args.signed else params,
                ).get("data", {})
                tracks = []
                for track in data.get("subtitle", {}).get("subtitles", []):
                    url = track.get("subtitle_url", "")
                    item = {
                        "language": track.get("lan"),
                        "aid_in_path": str(aid) in urlsplit(url).path,
                        "cid_in_path": str(cid) in urlsplit(url).path,
                    }
                    try:
                        subtitle_url(url, aid, cid)
                        item["binding_valid"] = True
                    except PlatformError as exc:
                        item.update(binding_valid=False, error=str(exc))
                    tracks.append(item)
                output = {
                    "aid": aid,
                    "cid": cid,
                    "response_aid": data.get("aid"),
                    "response_cid": data.get("cid"),
                    "tracks": tracks,
                }
                if args.verify_text:
                    text = client.transcript(args.bvid, cid, aid)
                    output["verified_text"] = (
                        " ".join(s["text"] for s in text["segments"])[:200] if text else None
                    )
            elif args.command == "folders":
                output = [
                    {"id": r["id"], "title": r["title"], "count": r.get("media_count")}
                    for r in client.folders()
                    if not args.name or r["title"] in args.name
                ]
            else:
                output = apply_plan(
                    client,
                    workspace,
                    read_json(args.plan),
                    args.confirm,
                    restore=args.command == "restore",
                )
                save_json(workspace.root / (args.command + "-receipt.json"), output)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except PlatformError as exc:
        print(
            json.dumps({"error": exc.kind, "message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "error": "configuration",
                    "message": "输入文件、配置或结构无效",
                    "exception": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
