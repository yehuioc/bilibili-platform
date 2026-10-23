"""AI/脚本的本机工具入口；只读本地令牌，不把凭据送入模型。"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def main(argv=None):
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description="调用B站后台插件动作")
    parser.add_argument("--workspace",default="private/library")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("actions")
    call=commands.add_parser("call")
    call.add_argument("action")
    call.add_argument("--args-file",type=Path)
    call.add_argument("--request-id",default=None)
    call.add_argument("--wait",type=float,default=60)
    for name in ("job","cancel"):
        p=commands.add_parser(name);p.add_argument("job_id")
    args=parser.parse_args(argv)
    try:
        root=Path(args.workspace)
        url=json.loads((root/"server.json").read_text(encoding="utf-8"))["url"]
        parsed=urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.path not in ("","/"):
            raise ValueError("只允许本项目本机后台")
        token=(root/"control-token.txt").read_text(encoding="utf-8").strip()
        with httpx.Client(base_url=url,trust_env=False,timeout=10,headers={"Authorization":"Bearer "+token}) as http:
            def post(body):
                response=http.post("/control",json=body)
                data=response.json()
                if not response.is_success:
                    raise ValueError(data.get("error","工具调用失败"))
                return data
            if args.command == "actions":
                value=post({"op":"catalog"})
            elif args.command == "call":
                data=json.loads(args.args_file.read_text(encoding="utf-8-sig")) if args.args_file else {}
                value=post({"op":"call","request":{"action":args.action,"args":data,"request_id":args.request_id or str(uuid.uuid4())}})
                deadline=time.monotonic()+max(0,min(args.wait,600))
                while time.monotonic()<deadline:
                    value=post({"op":"job","job_id":value.get("job_id",value.get("id"))})
                    if value["status"] not in ("running","queued"):
                        break
                    time.sleep(.3)
            else:
                value=post({"op":args.command,"job_id":args.job_id})
        print(json.dumps(value,ensure_ascii=False,indent=2))
        return 0 if value.get("status") not in ("failed","interrupted","cancelled") and (value.get("result") or {}).get("status") != "partial" else 2
    except (OSError,ValueError,httpx.HTTPError):
        print(json.dumps({"error":"本机工具调用失败，请核对服务、参数和任务状态"},ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
