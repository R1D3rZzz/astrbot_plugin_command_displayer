"""插件/命令模糊匹配：归一化、插件名匹配、命令名验证"""

from typing import List, Optional

from .models import IndexPlugin


def _normalize(s: str) -> str:
    """归一化名称用于模糊比较：去空格、去常见分隔符、转小写"""
    return s.lower().replace(" ", "").replace("_", "").replace("-", "").replace("（", "(").replace("）", ")")


# 子串匹配的最小长度阈值：猜测词和插件名归一化后至少 2 个字符才允许子串匹配
_MIN_SUBSTRING_LEN = 2


def _match_plugin(guess: str, plugins: List[IndexPlugin]) -> Optional[str]:
    """
    将 LLM 返回的插件名匹配到实际插件，返回缓存 key（p["key"] 或 p["name"]）。
    支持多种模糊策略，按精确度从高到低依次尝试。
    """
    if not guess:
        return None
    guess = guess.strip()

    def _get_key(p: IndexPlugin) -> str:
        return p.get("key", "") or p.get("name", "")

    # 策略 1：精确匹配 name（忽略大小写）
    for p in plugins:
        pname = p.get("name", "")
        if pname and (pname == guess or pname.lower() == guess.lower()):
            return _get_key(p)

    # 策略 2：归一化匹配 name（忽略空格、分隔符、大小写）
    norm_guess = _normalize(guess)
    for p in plugins:
        pname = p.get("name", "")
        if pname and _normalize(pname) == norm_guess:
            return _get_key(p)

    # 策略 3：子串匹配 name（带长度阈值，防止短查询误命中）
    if len(norm_guess) >= _MIN_SUBSTRING_LEN:
        for p in plugins:
            pname = p.get("name", "")
            if not pname:
                continue
            pnorm = _normalize(pname)
            if not pnorm:
                continue
            # 双向子串匹配，但要求较短的一方长度 >= 阈值
            shorter = min(len(norm_guess), len(pnorm))
            if shorter >= _MIN_SUBSTRING_LEN and (pnorm in norm_guess or norm_guess in pnorm):
                return _get_key(p)

    return None


def _match_command(guess: str, plugin_key: str, plugins: List[IndexPlugin]) -> Optional[str]:
    """验证命令是否存在于指定插件中（plugin_key 可以是 name 或 key）。
    支持归一化匹配：忽略 / 前缀、大小写、空格差异。
    """
    norm_guess = _normalize(guess)
    for p in plugins:
        if p.get("name") == plugin_key or p.get("key", "") == plugin_key:
            for cmd in p.get("commands", []):
                cmd_name = cmd.get("command", "")
                # 精确匹配
                if cmd_name.lower() == guess.lower():
                    return cmd_name
                # 归一化匹配（忽略 / 前缀、大小写、空格）
                if _normalize(cmd_name) == norm_guess:
                    return cmd_name
            return None
    return None


def _find_plugin(pk: str, plugins: List[IndexPlugin]) -> Optional[IndexPlugin]:
    """按 key 或 name 查找插件"""
    for p in plugins:
        if p.get("key", "") == pk or p.get("name", "") == pk:
            return p
    return None


def _find_command(cn: str, pinfo: IndexPlugin) -> Optional[dict]:
    """在插件中按命令名查找命令"""
    for cmd in pinfo.get("commands", []):
        if cmd.get("command", "") == cn:
            return cmd
    return None
