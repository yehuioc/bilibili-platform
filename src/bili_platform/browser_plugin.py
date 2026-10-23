from .plugins import Action, Plugin
from .runtime import object_schema


def plugin():
    actions = []
    definitions = [
        ("snapshot","读取已绑定B站标签页的实际标题、地址、BVID、可见文本与可导航链接",object_schema()),
        ("navigate","在绑定标签页打开B站视频页或收藏页",object_schema({"url":{"type":"string","maxLength":2048}},("url",))),
        ("scroll","滚动绑定页面并返回实际位置",object_schema({"y":{"type":"integer","minimum":-1600,"maximum":1600}},("y",))),
        ("click","点击最近快照中的B站导航链接，不开放点赞、评论等写操作",object_schema({"ref":{"type":"string","maxLength":80},"page_key":{"type":"string","maxLength":80}},("ref","page_key"))),
        ("player","控制视频播放、暂停或跳转并返回真实播放状态",object_schema({"operation":{"type":"string","enum":["play","pause","seek"]},"seconds":{"type":"number","minimum":0,"maximum":86400}},("operation",))),
    ]
    for name, desc, schema in definitions:
        def handler(ctx,args, action=name):
            return ctx.browser(action,args)
        actions.append(Action("browser."+name,desc,schema,handler,"browser"))
    return Plugin("browser",1,tuple(d[0] for d in definitions),tuple(actions))
