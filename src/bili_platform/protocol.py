"""平台协议与确定性错误语义，独立于归档和模型服务。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import PurePosixPath
from urllib.parse import urlencode, urlsplit

import httpx

API = "https://api.bilibili.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
MIXIN = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


class PlatformError(RuntimeError):
    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def json_response(response: httpx.Response, *, envelope=True):
    status = response.status_code
    if status in (401, 403):
        raise PlatformError("auth", "B站登录或访问权限失效")
    if status in (412, 429):
        raise PlatformError("rate_limit", "B站限流或风控；停止本轮请求")
    if status != 200:
        raise PlatformError(
            "transient" if status >= 500 else "permanent", f"B站请求失败 HTTP {status}"
        )
    try:
        data = response.json()
    except (ValueError, UnicodeError) as exc:
        raise PlatformError("transient", "B站返回非 JSON 数据") from exc
    if not isinstance(data, dict):
        raise PlatformError("transient", "B站响应结构变化：预期 JSON 对象")
    if envelope:
        code = data.get("code")
        if type(code) is not int:
            raise PlatformError("transient", "B站响应缺少有效 code")
        if code in (-101, -111):
            raise PlatformError("auth", "B站认证失效")
        if code in (-352, -412, -429):
            raise PlatformError("rate_limit", "B站限流或风控；停止本轮请求")
        if code:
            raise PlatformError("permanent", f"B站业务错误 code={code}")
    return data


def mixin_key(nav: dict) -> str:
    images = nav.get("wbi_img")
    if not isinstance(images, dict):
        raise PlatformError("platform_limited", "缺少 WBI 签名材料")
    key = "".join(
        PurePosixPath(urlsplit(str(images.get(k, ""))).path).stem for k in ("img_url", "sub_url")
    )
    if len(key) < 64:
        raise PlatformError("platform_limited", "WBI 签名材料格式变化")
    return "".join(key[i] for i in MIXIN)[:32]


def sign_wbi(params: dict, key: str, now=None) -> dict:
    values = {**params, "wts": int(time.time() if now is None else now)}
    values = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(values.items())}
    values["w_rid"] = hashlib.md5((urlencode(values) + key).encode()).hexdigest()
    return values


def subtitle_url(value: str, aid=None, cid=None) -> str:
    if value.startswith("//"):
        value = "https:" + value
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port not in (None, 443)
        or not any(host == d or host.endswith("." + d) for d in ("hdslb.com", "bilibili.com"))
    ):
        raise PlatformError("permanent", "字幕地址不属于受支持的 B站 HTTPS 来源")
    # 实测播放器可能返回正确身份字段但搭配其他视频字幕；响应身份不足以放行哈希地址。
    from urllib.parse import parse_qs

    query = parse_qs(url.query)
    for name, expected in (("aid", aid), ("cid", cid)):
        if expected is not None and name in query and query[name] != [str(expected)]:
            raise PlatformError("identity", "字幕地址声明的内容身份与请求不符")
        # 平台 CDN 可能将 aid/cid 连续编码在同一路径，不能假设数字之间有分隔符。
        if (
            expected is not None
            and query.get(name) != [str(expected)]
            and str(expected) not in url.path
        ):
            raise PlatformError("identity", "字幕地址无法核对目标视频与分P身份")
    return value
