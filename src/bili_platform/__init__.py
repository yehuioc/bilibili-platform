"""公共接口；插件不得自行复制 B 站鉴权、请求和分页协议。"""

from .client import BilibiliClient
from .protocol import PlatformError

__all__ = ["BilibiliClient", "PlatformError"]
