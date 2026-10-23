"""唯一平台接口实现；业务数据持久化由消费者负责。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from .protocol import (
    API,
    HEADERS,
    PlatformError,
    fingerprint,
    json_response,
    mixin_key,
    sign_wbi,
    subtitle_url,
)


class BilibiliClient:
    def __init__(self, cookies=(), *, http=None, public_http=None, pace=None, clock=time.time):
        jar = httpx.Cookies()
        self.csrf = ""
        for row in cookies:
            domain = str(row.get("domain", "")).lstrip(".").lower()
            if domain != "bilibili.com" and not domain.endswith(".bilibili.com"):
                continue
            name, value = str(row.get("name", "")), str(row.get("value", ""))
            if name and value:
                jar.set(name, value, domain=str(row["domain"]), path=str(row.get("path", "/")))
                if name == "bili_jct":
                    self.csrf = value
        self.http = http or httpx.Client(
            headers=HEADERS, cookies=jar, timeout=30, follow_redirects=False
        )
        self.public_http = public_http or httpx.Client(
            headers=HEADERS, timeout=30, follow_redirects=False
        )
        self._own_http, self._own_public = http is None, public_http is None
        self.pace = pace or self._default_pace
        self.clock = clock
        self._last_request = None
        self.identity = None
        self.key = None

    @classmethod
    def from_cookie_file(cls, path, **kwargs):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data.get("cookies", []) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise PlatformError("configuration", "Cookie 文件不是记录数组")
        return cls(rows, **kwargs)

    def close(self):
        if self._own_http:
            self.http.close()
        if self._own_public:
            self.public_http.close()

    def _default_pace(self):
        now = time.monotonic()
        if self._last_request is not None:
            time.sleep(max(0, 1.1 - (now - self._last_request)))
        self._last_request = time.monotonic()

    def request(self, path, params=None, *, pace=True, headers=None, method="GET", form=None):
        if not path.startswith("/x/") or "?" in path or "#" in path:
            raise PlatformError("configuration", "只允许相对 B站 API 路径")
        if pace:
            self.pace()
        try:
            response = self.http.request(
                method,
                API + path,
                params=params or {},
                data=form,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            # 写操作结果未知；调用者必须读回核对，不能在这里自动重试。
            kind = "write_uncertain" if method != "GET" else "transient"
            raise PlatformError(kind, "B站请求未获得确定结果；写操作需先读回核对") from exc
        try:
            return json_response(response)
        except PlatformError as exc:
            if method != "GET" and exc.kind == "transient":
                raise PlatformError(
                    "write_uncertain", "写请求响应无法确认；必须先读回核对"
                ) from exc
            raise

    def nav(self):
        data = self.request("/x/web-interface/nav").get("data")
        if not isinstance(data, dict) or not data.get("isLogin") or not data.get("mid"):
            raise PlatformError("auth", "未取得已登录账号身份")
        self.identity = {"mid": int(data["mid"])}
        try:
            self.key = mixin_key(data)
        except PlatformError:
            pass
        return self.identity

    def signed(self, params):
        if not self.key:
            data = self.request("/x/web-interface/nav").get("data", {})
            self.key = mixin_key(data)
        return sign_wbi(params, self.key, self.clock())

    def folders(self):
        mid = (self.identity or self.nav())["mid"]
        data = self.request("/x/v3/fav/folder/created/list-all", {"up_mid": mid, "type": 2}).get(
            "data"
        )
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise PlatformError("transient", "收藏夹列表结构变化，不能按空列表处理")
        rows = data["list"]
        if not all(
            isinstance(r, dict)
            and str(r.get("id", "")).isdigit()
            and isinstance(r.get("title"), str)
            for r in rows
        ):
            raise PlatformError("transient", "收藏夹条目结构变化")
        return rows

    def resolve_folder(self, name):
        rows = [r for r in self.folders() if r["title"] == name]
        if len(rows) != 1:
            raise PlatformError("identity", f"收藏夹名称未唯一匹配：{name}")
        return rows[0]

    def favorite_page(self, folder_id, page):
        data = self.request(
            "/x/v3/fav/resource/list",
            {
                "media_id": int(folder_id),
                "pn": page,
                "ps": 20,
                "order": "mtime",
                "keyword": "",
                "type": 0,
                "tid": 0,
                "platform": "web",
            },
        ).get("data")
        if not isinstance(data, dict) or "has_more" not in data:
            raise PlatformError("transient", "收藏分页结构变化")
        rows = data.get("medias")
        if rows is None and not data["has_more"]:
            rows = []
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise PlatformError("transient", "收藏分页内容无效")
        return rows, bool(data["has_more"])

    def inventory(self, folder, *, limit=0, max_pages=2000):
        result, seen_ids, seen_pages = [], set(), set()
        for page in range(1, max_pages + 1):
            rows, more = self.favorite_page(folder["id"], page)
            signature = fingerprint(rows)
            if more and (not rows or signature in seen_pages):
                raise PlatformError("pagination", "分页没有前进，停止而不宣称读取完整")
            seen_pages.add(signature)
            for row in rows:
                aid = row.get("id") or row.get("aid")
                if not aid:
                    raise PlatformError("identity", "收藏条目缺少平台资源 ID")
                key = (int(aid), int(row.get("type", 2)))
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                result.append(row)
                if limit and len(result) >= limit:
                    return result, not more and row is rows[-1]
            if not more:
                return result, True
        raise PlatformError("pagination", "达到最大页数，不能声称读取完整")

    def members(self, folder):
        """实时紧凑成员表；关系核对无需重复下载所有视频的详情分页。"""
        rows = self.request('/x/v3/fav/resource/ids',
                            {'media_id':int(folder['id']),'platform':'web'}).get('data')
        if not isinstance(rows,list):
            raise PlatformError('pagination','收藏成员清单结构变化，停止写入')
        result,seen=[],set()
        for row in rows:
            if not isinstance(row,dict) or type(row.get('id')) is not int or row['id']<=0 or type(row.get('type')) is not int:
                raise PlatformError('identity','收藏成员缺少明确身份')
            key=(row['id'],row['type'])
            if key in seen:
                raise PlatformError('pagination','收藏成员清单重复，不能确认完整')
            seen.add(key)
            bvid=row.get('bvid') or row.get('bv_id')
            if bvid and not re.fullmatch(r'BV[A-Za-z0-9]{10}',str(bvid)):
                raise PlatformError('identity','收藏成员视频身份无效')
            result.append({**row,'bvid':bvid})
        # 此接口声明返回完整清单；与本次新读取的夹计数不符时停止而非猜测缺失。
        count=folder.get('media_count')
        if type(count) is int and len(result)!=count:
            raise PlatformError('pagination','收藏成员数量已变化，请重新读取后再操作')
        return result

    def video(self, bvid):
        data = self.request("/x/web-interface/view", {"bvid": bvid}).get("data")
        if not isinstance(data, dict) or data.get("bvid") != bvid:
            raise PlatformError("identity", "视频详情身份不匹配")
        return data

    def playback(self, bvid, cid):
        data=self.request('/x/player/wbi/playurl',self.signed({
            'bvid':bvid,'cid':int(cid),'qn':64,'fnval':1,'fnver':0,'fourk':0})).get('data')
        if not isinstance(data,dict) or not isinstance(data.get('durl'),list) or len(data['durl'])!=1:
            raise PlatformError('platform_limited','当前没有可用的单段MP4材料，未绕过播放权限')
        row=data['durl'][0]
        return {'url':row.get('url'),'urls':[row.get('url'),*(row.get('backup_url') or [])],'size':row.get('size'),
                'duration_ms':data.get('timelength'),'quality':data.get('quality')}

    def transcript(self, bvid, cid, aid=None, *, duration=None):
        tracks, received, last_error = [], False, None
        binding_error = None
        player_bound = False
        for endpoint in ("/x/player/v2", "/x/player/wbi/v2"):
            params = {"bvid": bvid, "cid": int(cid)}
            if aid:
                params["aid"] = aid
            try:
                data = self.request(
                    endpoint, self.signed(params) if "/wbi/" in endpoint else params
                ).get("data")
            except PlatformError as exc:
                if exc.kind in ("auth", "rate_limit"):
                    raise
                last_error = exc
                continue
            if not isinstance(data, dict):
                raise PlatformError("transient", "播放器响应结构变化")
            if data.get("bvid") is not None and data["bvid"] != bvid:
                raise PlatformError("identity", "播放器BVID身份不符")
            if data.get("cid") is not None and str(data["cid"]) != str(cid):
                raise PlatformError("identity", "播放器分P身份不符")
            if aid is not None and data.get("aid") is not None and str(data["aid"]) != str(aid):
                raise PlatformError("identity", "播放器视频身份不符")
            received = True
            player_bound = (aid is not None and data.get('bvid') == bvid
                            and str(data.get('aid')) == str(aid)
                            and str(data.get('cid')) == str(cid))
            subtitle = data.get("subtitle", {})
            if not isinstance(subtitle, dict):
                raise PlatformError("transient", "字幕容器结构变化")
            subtitles = subtitle.get("subtitles", [])
            if not isinstance(subtitles, list):
                raise PlatformError("transient", "字幕轨道结构变化")
            tracks = []
            for row in subtitles:
                if not isinstance(row, dict) or not row.get("subtitle_url"):
                    continue
                try:
                    subtitle_url(str(row["subtitle_url"]), aid, cid)
                except PlatformError as exc:
                    binding_error = exc
                    continue
                tracks.append(row)
            if tracks:
                break
        if not received and last_error:
            raise last_error
        if not tracks:
            if binding_error:
                raise binding_error
            return None
        tracks.sort(
            key=lambda t: (
                0 if str(t.get("lan", "")).startswith("zh") else 1 if t.get("lan") == "ai-zh" else 2
            )
        )
        if player_bound and duration is None:
            detail = self.video(bvid)
            matching = [p for p in detail.get('pages', []) if str(p.get('cid')) == str(cid)]
            if len(matching) != 1:
                raise PlatformError('identity', '字幕分P无法对应已核对视频')
            duration = matching[0]['duration']
        for track in tracks:
            url = subtitle_url(str(track["subtitle_url"]), aid, cid)
            self.pace()
            try:
                response = self.public_http.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise PlatformError("transient", "字幕传输失败") from exc
            payload = json_response(response, envelope=False)
            body = payload.get("body")
            if not isinstance(body, list):
                raise PlatformError("transient", "字幕正文结构变化")
            if str(track.get("lan", "")).startswith("ai-"):
                self.pace()
                try:
                    repeated = json_response(
                        self.public_http.get(url, follow_redirects=False), envelope=False
                    )
                except httpx.HTTPError as exc:
                    raise PlatformError("transient", "字幕复核传输失败") from exc
                if repeated.get("body") != body:
                    raise PlatformError("identity", "AI字幕重复读取不一致，不能作为分类证据")
            segments = []
            for row in body:
                if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                    raise PlatformError("transient", "字幕行结构变化")
                if row["content"].strip():
                    start, end = row.get("from"), row.get("to")
                    if (start is not None or end is not None) and (
                        type(start) not in (int, float)
                        or type(end) not in (int, float)
                        or not 0 <= start <= end
                    ):
                        raise PlatformError("transient", "字幕时间范围无效")
                    if duration is not None and end is not None and end > duration + 2:
                        raise PlatformError('identity', '字幕时间超过视频分P时长，疑似串稿，拒绝使用')
                    segments.append(
                        {
                            "start": row.get("from"),
                            "end": row.get("to"),
                            "text": row["content"].strip(),
                        }
                    )
            if segments:
                return {
                    "bvid": bvid,
                    "cid": int(cid),
                    "language": track.get("lan_doc", ""),
                    "source": "ai_subtitle"
                    if str(track.get("lan", "")).startswith("ai-")
                    else "subtitle",
                    "identity_verified": True,
                    "identity_binding": "url_ids",
                    "segments": segments,
                    "raw": payload,
                }
        return None

    def create_folder(self, title):
        if not self.csrf:
            raise PlatformError("auth", "收藏写入缺少 CSRF 凭证")
        if not title.strip() or len(title) > 80:
            raise PlatformError("configuration", "收藏夹名称无效")
        data = self.request(
            "/x/v3/fav/folder/add",
            method="POST",
            form={
                "title": title,
                "intro": "",
                "privacy": 1,
                "csrf": self.csrf,
            },
        ).get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise PlatformError("write_uncertain", "创建结果缺少 ID；必须先读回")
        return data

    def move(self, source_id, target_id, aid, resource_type=2):
        if not self.csrf:
            raise PlatformError("auth", "收藏写入缺少 CSRF 凭证")
        mid = (self.identity or self.nav())["mid"]
        self.request(
            "/x/v3/fav/resource/move",
            method="POST",
            form={
                "src_media_id": source_id,
                "tar_media_id": target_id,
                "mid": mid,
                "resources": f"{int(aid)}:{int(resource_type)}",
                "csrf": self.csrf,
            },
        )

    def copy(self, source_id, target_id, aid, resource_type=2):
        if not self.csrf:
            raise PlatformError("auth", "收藏写入缺少 CSRF 凭证")
        mid = (self.identity or self.nav())["mid"]
        self.request("/x/v3/fav/resource/copy", method="POST", form={
            "src_media_id":int(source_id),"tar_media_id":int(target_id),"mid":mid,
            "resources":f"{int(aid)}:{int(resource_type)}","platform":"web","csrf":self.csrf})

    def remove_from_folder(self, folder_id, aid, resource_type=2):
        if not self.csrf:
            raise PlatformError("auth", "收藏写入缺少 CSRF 凭证")
        self.request("/x/v3/fav/resource/batch-del", method="POST", form={
            "media_id":int(folder_id),"resources":f"{int(aid)}:{int(resource_type)}",
            "platform":"web","csrf":self.csrf})

    def delete_folder(self, folder_id):
        if not self.csrf:
            raise PlatformError("auth", "收藏写入缺少 CSRF 凭证")
        self.request("/x/v3/fav/folder/del",method="POST",form={"media_ids":str(int(folder_id)),"csrf":self.csrf})
