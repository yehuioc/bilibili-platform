"""收藏插件：确定范围后预览、确认、执行、读回；不依赖内容分类。"""
from .plugins import Action, Plugin
from .protocol import PlatformError, fingerprint
from .runtime import object_schema

ID = {"type":"integer","minimum":1}
BVID = {"type":"string","pattern":r"BV[A-Za-z0-9]{10}","maxLength":12}


def folders(ctx, args):
    client = ctx.platform()
    try:
        return {"account_mid":client.identity["mid"],"folders":[{"id":int(r["id"]),"title":r["title"],"count":r.get("media_count")} for r in client.folders()]}
    finally:
        client.close()


def owned(client, folder_id, folders=None):
    row = next((r for r in (folders if folders is not None else client.folders()) if int(r["id"]) == folder_id),None)
    if not row:
        raise PlatformError("identity","收藏夹不属于当前账号或已不存在")
    return row


def resources(client, folder, *, fresh_count=True):
    if hasattr(client,'members'):
        # 关系写入后计数可能已变，不能再使用写入前的夹计数。
        identity=folder if fresh_count else {k:v for k,v in folder.items() if k!='media_count'}
        rows,complete=client.members(identity),True
    else:
        rows, complete = client.inventory(folder)
    if not complete:
        raise PlatformError("pagination","收藏关系未读完整，停止写入")
    return {int(r.get("id") or r["aid"]):r for r in rows if int(r.get("type",2)) == 2}


def videos(ctx,args):
    client = ctx.platform()
    try:
        folder = owned(client,args["folder_id"])
        rows, complete = client.inventory(folder,limit=args.get("limit",20))
        return {"account_mid":client.identity["mid"],"folder":{"id":int(folder["id"]),"title":folder["title"]},"complete":complete,
                "videos":[{"bvid":r.get("bvid"),"aid":int(r.get("id") or r["aid"]),"title":r.get("title",""),"type":r.get("type",2)} for r in rows]}
    finally:
        client.close()


def create(ctx,args):
    title = args["title"].strip()
    if not title or args["confirmation"] != title:
        raise PlatformError("authorization","创建须确认具体收藏夹名称")
    client = ctx.platform()
    try:
        if any(r["title"] == title for r in client.folders()):
            raise PlatformError("identity","同名收藏夹已存在，请使用现有收藏夹ID")
        ctx.check()
        ctx.checkpoint({"phase":"create_submitted","account_mid":client.identity["mid"],"title":title})
        response_id = None
        try:
            response_id = int(client.create_folder(title)["id"])
        except PlatformError as exc:
            if exc.kind != "write_uncertain":
                raise
        matches = [r for r in client.folders() if r["title"] == title and (not response_id or int(r["id"]) == response_id)]
        if len(matches) != 1:
            raise PlatformError("write_uncertain","未能唯一读回创建结果，不自动重建")
        row = matches[0]
        if "attr" in row and not int(row["attr"]) & 2:
            raise PlatformError("write_uncertain","已创建但未确认私密属性，请核对")
        return {"status":"verified","account_mid":client.identity["mid"],"folder":{"id":int(row["id"]),"title":title},"privacy":"private"}
    finally:
        client.close()


def prepare(ctx,args):
    if args["source_id"] == args["target_id"] or len(set(args["bvids"])) != len(args["bvids"]):
        raise ValueError("源目标必须不同，视频不能重复")
    client = ctx.platform()
    try:
        current=client.folders()
        source,target = owned(client,args["source_id"],current),owned(client,args["target_id"],current)
        source_rows,target_rows = resources(client,source),resources(client,target)
        items=[]
        for bvid in args["bvids"]:
            ctx.check()
            detail = client.video(bvid)
            aid = int(detail["aid"])
            if aid not in source_rows or source_rows[aid].get("bvid") != bvid:
                raise PlatformError("identity","选中视频不在指定来源夹或视频身份已变")
            items.append({"bvid":bvid,"aid":aid,"title":detail["title"],"target_existed":aid in target_rows})
        value={"account_mid":client.identity["mid"],"mode":args["mode"],
               "source":{"id":int(source["id"]),"title":source["title"]},
               "target":{"id":int(target["id"]),"title":target["title"]},"items":items}
        return {**value,"confirmation":fingerprint(value)}
    finally:
        client.close()


def execute(ctx,args):
    plan = ctx.result(args["plan_id"],"favorites.prepare")
    if args["confirmation"] != plan["confirmation"] or fingerprint({k:v for k,v in plan.items() if k != "confirmation"}) != plan["confirmation"]:
        raise PlatformError("authorization","确认值与实际预览不一致")
    client = ctx.platform()
    results=[]
    try:
        if client.identity["mid"] != plan["account_mid"]:
            raise PlatformError("identity","登录账号与预览不一致")
        current=client.folders()
        source,target=owned(client,plan["source"]["id"],current),owned(client,plan["target"]["id"],current)
        if source["title"] != plan["source"]["title"] or target["title"] != plan["target"]["title"]:
            raise PlatformError("identity","收藏夹名称已变，请重新预览")
        srows,trows=resources(client,source),resources(client,target)
        for item in plan["items"]:
            ctx.check()
            aid=item["aid"]
            if aid not in srows:
                # 已完成的移动可以只读确认；从未到达目标的条目不能继续。
                results.append({**item,"status":"verified" if aid in trows and plan["mode"] == "move" else "conflict"})
                continue
            if srows[aid].get("bvid") != item["bvid"]:
                raise PlatformError("identity","执行前视频身份发生变化")
            if (aid in trows) != item["target_existed"]:
                raise PlatformError("identity","预览后收藏关系变化，请重新预览")
            ctx.checkpoint({"phase":"copy_before_remove","plan_id":args["plan_id"],"items":results,"current":item})
            if aid not in trows:
                ctx.check()
                try:
                    client.copy(source["id"],target["id"],aid)
                except PlatformError as exc:
                    if exc.kind != "write_uncertain":
                        raise
                trows=resources(client,target,fresh_count=False)
                if aid not in trows or trows[aid].get("bvid") != item["bvid"]:
                    results.append({**item,"status":"unverified","phase":"copy"})
                    break
            if plan["mode"] == "move":
                ctx.check()
                ctx.checkpoint({"phase":"remove_source_submitted","plan_id":args["plan_id"],"items":results,"current":item})
                try:
                    client.remove_from_folder(source["id"],aid)
                except PlatformError as exc:
                    if exc.kind != "write_uncertain":
                        raise
            srows,trows=resources(client,source,fresh_count=False),resources(client,target,fresh_count=False)
            okay=aid in trows and ((aid not in srows) if plan["mode"] == "move" else aid in srows)
            results.append({**item,"status":"verified" if okay else "unverified"})
            ctx.checkpoint({"phase":"verified_item","plan_id":args["plan_id"],"items":results})
            if not okay:
                break
        handled={r["bvid"] for r in results}
        results += [{**r,"status":"not_attempted"} for r in plan["items"] if r["bvid"] not in handled]
        return {"status":"verified" if all(r["status"] == "verified" for r in results) else "partial",
                "mode":plan["mode"],"account_mid":plan["account_mid"],"source":plan["source"],"target":plan["target"],"items":results}
    finally:
        client.close()


def plugin():
    return Plugin("favorites",1,("classify","preview","move","restore","folders","videos","create","prepare","execute"),(
        Action("favorites.folders","读取当前后台账号及其收藏夹",object_schema(),folders),
        Action("favorites.videos","读取指定收藏夹的视频",object_schema({"folder_id":ID,"limit":{"type":"integer","minimum":1,"maximum":100}},("folder_id",)),videos),
        Action("favorites.create","创建并读回一个私密收藏夹",object_schema({"title":{"type":"string","minLength":1,"maxLength":80},"confirmation":{"type":"string","maxLength":80}},("title","confirmation")),create,"write"),
        Action("favorites.prepare","只读生成复制或移动的具体预览",object_schema({"source_id":ID,"target_id":ID,"mode":{"type":"string","enum":["copy","move"]},"bvids":{"type":"array","items":BVID,"minItems":1,"maxItems":20}},("source_id","target_id","mode","bvids")),prepare),
        Action("favorites.execute","确认预览后执行，复制验证后才移除来源",object_schema({"plan_id":{"type":"string","maxLength":40},"confirmation":{"type":"string","maxLength":64}},("plan_id","confirmation")),execute,"write"),
    ))
