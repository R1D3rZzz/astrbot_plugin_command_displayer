"""统一数据格式定义和常量"""

from typing import Dict, TypedDict, List

# ═══════════════════════════════════════════════════════
# 数据格式
# ═══════════════════════════════════════════════════════


class CommandEntry(TypedDict, total=False):
    """单条命令信息"""
    command: str          # 命令名，如 "/天气"
    args: str             # 参数说明，如 "[城市名]"，无则空串
    description: str      # 命令功能说明
    source: str           # 数据来源：SOURCE_DIRECT / SOURCE_LLM
    filter_type: str      # 过滤器类型（仅直接读取时有值）
    aliases: List[str]    # 别名列表（仅直接读取时有值）
    is_regex: bool        # 是否正则匹配（仅直接读取时有值）


class PluginInfo(TypedDict, total=False):
    """插件信息"""
    name: str             # 插件显示名
    description: str      # 插件描述
    commands: List[CommandEntry]  # 命令列表
    source: str           # 数据来源：SOURCE_DIRECT / SOURCE_LLM


# ═══════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════

# 数据来源
SOURCE_DIRECT = "direct"
SOURCE_LLM = "llm"

# 数据来源标记
SOURCE_TAGS: Dict[str, str] = {
    SOURCE_DIRECT: "[直接]",
    SOURCE_LLM: "[LLM]",
}

# 过滤器类型标记
FILTER_TYPE_TAGS: Dict[str, str] = {
    "command": "[指令]",
    "regex": "[正则]",
    "on_all_message": "[监听]",
    "PlatformMessageFilter": "[平台]",
    "EventFilter": "[事件]",
    "CommandGroupFilter": "[指令组]",
}

# 日志级别映射
LOG_LEVEL_MAP = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

# 缓存文件路径
CACHE_FILE_PATH = "data/command_displayer/cache.json"


# ═══════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════


def name_matches(a: str, b: str) -> bool:
    """名称匹配（忽略大小写和常见分隔符差异）"""
    norm = lambda s: s.lower().replace("_", "").replace("-", "").replace(" ", "")
    return norm(a) == norm(b)


def name_in_data(dir_name: str, data: Dict[str, PluginInfo]) -> bool:
    """检查目录名是否已存在于数据中"""
    for key in data:
        if key == dir_name or name_matches(key, dir_name):
            return True
    return False
