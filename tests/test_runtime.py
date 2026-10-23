import copy
import threading
import time
import uuid

import pytest

from bili_platform.extension_bridge import ExtensionBridge
from bili_platform.library import Library
from bili_platform.protocol import PlatformError
from bili_platform.runtime import Runtime

ORIGIN="chrome-extension://"+"c"*32
BVID="BV1AbCdEfGhJ"


@pytest.fixture
def system(tmp_path):
    state={"mid":42,"folders":{7:{"id":7,"title":"来源"},8:{"id":8,"title":"目标"}},
           "rows":{7:{1:{"id":1,"type":2,"bvid":BVID,"title":"视频"}},8:{}},"calls":[],"drop_copy":False}

    class Client:
        def __init__(self):
            self.identity={"mid":state["mid"]}

        def folders(self):
            return list(state["folders"].values())

        def inventory(self,folder,**kwargs):
            return copy.deepcopy(list(state["rows"][folder["id"]].values())),True

        def members(self,folder):
            state.setdefault('member_reads',[]).append(folder['id'])
            return copy.deepcopy(list(state['rows'][folder['id']].values()))

        def video(self,bvid):
            return {"aid":1,"bvid":bvid,"title":"视频"}

        def copy(self,source,target,aid):
            state["calls"].append(("copy",source,target,aid))
            if not state["drop_copy"]:
                state["rows"][target][aid]=copy.deepcopy(state["rows"][source][aid])
            if state.get("uncertain"):
                raise PlatformError("write_uncertain","响应丢失")
            if state.get("after_copy"):
                state["after_copy"]()

        def remove_from_folder(self,folder,aid):
            state["calls"].append(("remove",folder,aid))
            state["rows"][folder].pop(aid,None)

        def create_folder(self,title):
            state["calls"].append(("create",title))
            row={"id":9,"title":title,"attr":2}
            state["folders"][9]=row;state["rows"][9]={}
            return row

        def close(self):
            pass

    lib=Library(tmp_path)
    bridge=ExtensionBridge(lib)
    runtime=Runtime(lib,bridge,Client)
    yield runtime,bridge,state
    runtime.close()


def call(runtime,action,args=None,request_id=None):
    result=runtime.submit({"action":action,"args":args or {},"request_id":request_id or str(uuid.uuid4())})
    end=time.monotonic()+5
    while time.monotonic()<end:
        job=runtime.job(result["job_id"])
        if job["status"] not in ("queued","running"):
            return job
        time.sleep(.01)
    pytest.fail("任务没有按时结束")


def prepare(runtime,mode="move"):
    job=call(runtime,"favorites.prepare",{"source_id":7,"target_id":8,"mode":mode,"bvids":[BVID]})
    assert job["status"]=="complete"
    return {"plan_id":job["id"],"confirmation":job["result"]["confirmation"]}


def test_move_copies_and_verifies_before_removing_source(system):
    runtime,_,state=system
    args=prepare(runtime)
    job=call(runtime,"favorites.execute",args)
    assert job["result"]["status"]=="verified"
    assert state["calls"]==[("copy",7,8,1),("remove",7,1)]
    assert 1 not in state["rows"][7] and 1 in state["rows"][8]
    assert state['member_reads'] and len(state['member_reads'])==7


def test_long_poll_wakes_immediately_for_a_command(system):
    _,bridge,_=system
    token=bridge.pair(ORIGIN,bridge.pairing_code()['code'])['token']
    b=binding();bridge.extension_request(ORIGIN,token,'poll',{'binding':b})
    values=[]
    thread=threading.Thread(target=lambda:values.append(bridge.extension_request(ORIGIN,token,'poll',{'binding':b,'wait':3})))
    thread.start()
    for _ in range(100):
        if bridge.waiting:break
        time.sleep(.005)
    assert bridge.waiting and bridge.state()['online']
    with bridge.condition:
        bridge.commands['test']={'id':'test','session':b['session'],'tab_id':12,'action':'snapshot','args':{},'expires':time.time()+10}
        bridge.condition.notify_all()
    thread.join(timeout=.5)
    assert not thread.is_alive() and values[0]['command']['id']=='test'


def test_old_waiting_poll_cannot_restore_replaced_binding(system):
    _,bridge,_=system
    token=bridge.pair(ORIGIN,bridge.pairing_code()['code'])['token']
    old=binding();bridge.extension_request(ORIGIN,token,'poll',{'binding':old})
    replies=[]
    thread=threading.Thread(target=lambda:replies.append(bridge.extension_request(ORIGIN,token,'poll',{'binding':old,'wait':1})))
    thread.start()
    for _ in range(100):
        if bridge.waiting:break
        time.sleep(.005)
    fresh=binding(control=False)
    bridge.extension_request(ORIGIN,token,'poll',{'binding':fresh})
    thread.join(timeout=2)
    assert not thread.is_alive() and bridge.binding['session']==fresh['session']
    assert not bridge.binding['control'] and replies[0]['command'] is None


def test_copy_preserves_source_and_idempotent_request(system):
    runtime,_,state=system
    args=prepare(runtime,"copy")
    first=call(runtime,"favorites.execute",args,request_id="same-copy-request")
    second=call(runtime,"favorites.execute",args,request_id="same-copy-request")
    assert first["id"]==second["id"] and state["calls"]==[("copy",7,8,1)]
    assert 1 in state["rows"][7] and 1 in state["rows"][8]
    with pytest.raises(ValueError,match="请求ID"):
        runtime.submit({"action":"favorites.folders","args":{},"request_id":"same-copy-request"})


def test_missing_target_after_copy_never_removes_source(system):
    runtime,_,state=system
    args=prepare(runtime);state["drop_copy"]=True
    job=call(runtime,"favorites.execute",args)
    assert job["result"]["status"]=="partial" and 1 in state["rows"][7]
    assert all(c[0]!="remove" for c in state["calls"])


def test_uncertain_copy_reconciles_without_repeating_post(system):
    runtime,_,state=system
    args=prepare(runtime);state["uncertain"]=True
    job=call(runtime,"favorites.execute",args)
    assert job["result"]["status"]=="verified"
    assert len([c for c in state["calls"] if c[0]=="copy"])==1


def test_account_or_relationship_change_blocks_writes(system):
    runtime,_,state=system
    args=prepare(runtime);state["mid"]=99
    job=call(runtime,"favorites.execute",args)
    assert job["status"]=="failed" and not state["calls"]
    state["mid"]=42;state["rows"][8][1]=copy.deepcopy(state["rows"][7][1])
    job=call(runtime,"favorites.execute",args)
    assert job["error"]["kind"]=="identity" and not state["calls"]


def test_cancel_after_verified_copy_keeps_source_and_checkpoint(system):
    runtime,_,state=system
    args=prepare(runtime)
    state["after_copy"]=lambda:runtime.cancel(next(j["id"] for j in runtime.jobs() if j["status"]=="running"))
    job=call(runtime,"favorites.execute",args)
    assert job["status"]=="cancelled" and job["result"]["phase"]=="copy_before_remove"
    assert 1 in state["rows"][7] and 1 in state["rows"][8]


def test_create_requires_exact_confirmation_and_rejects_existing(system):
    runtime,_,state=system
    bad=call(runtime,"favorites.create",{"title":"新夹","confirmation":"别的"})
    assert bad["status"]=="failed" and not state["calls"]
    good=call(runtime,"favorites.create",{"title":"新夹","confirmation":"新夹"})
    assert good["result"]["status"]=="verified" and good["result"]["folder"]["id"]==9
    assert call(runtime,"favorites.create",{"title":"新夹","confirmation":"新夹"})["status"]=="failed"
    assert len(state["calls"])==1


def test_action_catalog_and_schema_are_shared_and_closed(system):
    runtime,_,_=system
    names={a["name"] for a in runtime.catalog()["actions"]}
    assert {"browser.snapshot","favorites.create","favorites.execute"}<=names
    for action,args in [("browser.execute",{}),("browser.scroll",{"y":999999}),("favorites.create",{"title":"夹","confirmation":"夹","cookie":"bad"})]:
        with pytest.raises(ValueError):
            runtime.submit({"action":action,"args":args,"request_id":str(uuid.uuid4())})


def binding(session=None,control=True):
    return {"tab_id":12,"session":session or str(uuid.uuid4()),"url":"https://www.bilibili.com/video/"+BVID,"control":control}


def test_browser_roundtrip_returns_actual_identity_and_stop_rejects_late_result(system):
    runtime,bridge,_=system
    token=bridge.pair(ORIGIN,bridge.pairing_code()["code"])["token"]
    b=binding()
    bridge.extension_request(ORIGIN,token,"poll",{"binding":b})
    request=runtime.submit({"action":"browser.snapshot","args":{},"request_id":str(uuid.uuid4())})
    command=None
    for _ in range(100):
        command=bridge.extension_request(ORIGIN,token,"poll",{"binding":b})["command"]
        if command:break
        time.sleep(.01)
    assert command and command["tab_id"]==12
    result={"action":"snapshot","tab_id":12,"page":{"url":b["url"],"title":"真实标题","bvid":BVID}}
    bridge.extension_request(ORIGIN,token,"result",{"id":command["id"],"session":b["session"],"result":result,"error":None})
    for _ in range(100):
        job=runtime.job(request["job_id"])
        if job["status"]=="complete":break
        time.sleep(.01)
    assert job["result"]["page"]["bvid"]==BVID
    errors=[]
    def waiting():
        try:bridge.browser_request("snapshot",{},timeout=2)
        except PlatformError as exc:errors.append(exc)
    thread=threading.Thread(target=waiting);thread.start()
    for _ in range(100):
        command=bridge.extension_request(ORIGIN,token,"poll",{"binding":b})["command"]
        if command:break
        time.sleep(.01)
    bridge.stop_browser()
    with pytest.raises(ValueError,match="失效"):
        bridge.extension_request(ORIGIN,token,"result",{"id":command["id"],"session":b["session"],"result":result,"error":None})
    # 旧心跳不能重新开启已停止的会话。
    assert bridge.extension_request(ORIGIN,token,"poll",{"binding":b})["stopped"]
    thread.join(timeout=3)
    assert errors and not bridge.binding["control"]
