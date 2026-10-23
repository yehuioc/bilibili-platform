"""功能插件发现；只加载明确安装的 Python 包，不下载或执行远程插件。"""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points

from .protocol import PlatformError


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    parameters: dict
    handler: Callable
    effect: str = "read"


@dataclass(frozen=True)
class Plugin:
    name: str
    api_version: int
    capabilities: tuple[str, ...]
    actions: tuple[Action, ...] = ()


def discover():
    from .browser_plugin import plugin as browser
    from .favorites_plugin import plugin as favorites
    from .video_reader import plugin as video
    result = {p.name: p for p in (browser(), favorites(), video())}
    for entry in entry_points(group="bili_platform.plugins"):
        plugin = entry.load()()
        if not isinstance(plugin, Plugin) or plugin.api_version != 1 or plugin.name in result:
            raise PlatformError("plugin", "插件接口版本、类型或名称不兼容")
        result[plugin.name] = plugin
    return result
