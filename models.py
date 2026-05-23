"""数据模型、常量与工具函数"""

from typing import Dict, List, TypedDict

# ── 数据来源常量 ──────────────────────────────────

SOURCE_DIRECT = "direct"
SOURCE_LLM = "llm"

SOURCE_TAGS: Dict[str, str] = {
    SOURCE_DIRECT: "[直接]",
    SOURCE_LLM: "[LLM]",
}

FILTER_TYPE_TAGS: Dict[str, str] = {
    "command": "[指令]",
    "regex": "[正则]",
    "on_all_message": "[监听]",
    "PlatformMessageFilter": "[平台]",
    "EventFilter": "[事件]",
    "CommandGroupFilter": "[指令组]",
}

LOG_LEVEL_MAP = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

# ── LLM 路由参数 ─────────────────────────────────

LLM_TEMPERATURE = 0.1          # 路由 LLM 温度
LLM_CONFIRM_TIMEOUT = 60       # 确认超时（秒）

# ── 缓存与扫描参数 ────────────────────────────────

DEFAULT_SCAN_INTERVAL = 300    # 后台扫描间隔（秒）
DEFAULT_CACHE_TIMEOUT = 30     # 缓存超时（分钟）
MAX_README_SIZE = 1024 * 1024  # README 最大 1MB
MAX_INDEX_CHARS = 8000         # 路由索引最大字符数
MAX_FULL_PROXY_CHARS = 16000   # 全权代理索引最大字符数

# ── 文件路径 ──────────────────────────────────────

CACHE_FILE_PATH = "data/command_displayer/cache.json"
INDEX_FILE_PATH = "data/command_displayer/command_index.json"

# ── 数据类型 ──────────────────────────────────────


class CommandEntry(TypedDict, total=False):
    """单条命令信息（内存缓存格式）"""
    command: str
    args: str
    args_description: str
    description: str
    source: str
    filter_type: str
    aliases: List[str]
    is_regex: bool


class PluginInfo(TypedDict, total=False):
    """插件信息（内存缓存格式）"""
    name: str
    description: str
    commands: List[CommandEntry]
    source: str


class IndexCommand(TypedDict, total=False):
    """命令索引条目（command_index.json 格式）"""
    command: str
    args: str
    args_description: str
    description: str
    filter_type: str


class IndexPlugin(TypedDict, total=False):
    """插件索引条目（command_index.json 格式）"""
    name: str
    key: str
    description: str
    source: str
    commands: List[IndexCommand]


# ── 工具函数 ──────────────────────────────────────


def name_matches(a: str, b: str) -> bool:
    """名称匹配（忽略大小写和常见分隔符）"""
    norm = lambda s: s.lower().replace("_", "").replace("-", "").replace(" ", "")
    return norm(a) == norm(b)


def name_in_data(dir_name: str, data: Dict[str, PluginInfo]) -> bool:
    """检查目录名是否已存在于数据中"""
    for key in data:
        if key == dir_name or name_matches(key, dir_name):
            return True
    return False
