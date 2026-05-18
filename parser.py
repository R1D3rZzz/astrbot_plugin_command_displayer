import json
import re
from typing import Dict, List, Optional
from astrbot.api import logger

from .models import PluginInfo, CommandEntry, SOURCE_LLM


async def parse_readme(content: str, plugin_dir_name: str, provider=None) -> Optional[PluginInfo]:
    """使用 LLM 解析 README，提取插件名称、描述和命令列表"""
    if not provider:
        logger.error(f"未配置 LLM 提供商，无法解析 {plugin_dir_name}")
        return None

    cleaned = _preprocess_readme(content)
    if not cleaned:
        logger.debug(f"README 预处理后为空: {plugin_dir_name}")
        return None

    return await _parse_with_llm(cleaned, plugin_dir_name, provider)


# ═══════════════════════════════════════════════════════
# README 预处理
# ═══════════════════════════════════════════════════════

# 无关章节标题模式（命中后截断其后内容）
_CUTOFF_PATTERNS = [
    r"^#{1,3}\s*(?:变更日志|更新日志|Changelog|CHANGELOG|更新记录)",
    r"^#{1,3}\s*(?:License|许可证|LICENSE)",
    r"^#{1,3}\s*(?:Contributing|贡献指南|CONTRIBUTING)",
    r"^#{1,3}\s*(?:Star History|Stars History)",
    r"^#{1,3}\s*(?:致谢|Acknowledgement|Credits|Thanks)",
    r"^#{1,3}\s*(?:开发|Development|Developer)",
]


def _preprocess_readme(content: str) -> str:
    if not content:
        return ""

    text = content

    # 去除无意义 token 消耗
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)   # HTML 注释
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)                # 图片
    text = re.sub(r"^\s*\[!\[.*?\]\(.*?\)\]\(.*?\)\s*$", "", text, flags=re.MULTILINE)  # 徽章

    # 截断无关章节
    for pattern in _CUTOFF_PATTERNS:
        match = re.search(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
        if match:
            text = text[:match.start()]

    # 过滤长代码块（保留短示例）
    text = re.sub(r"```[\s\S]*?```", lambda m: m.group(0) if len(m.group(0)) < 500 else "", text)

    # 压缩空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ═══════════════════════════════════════════════════════
# LLM 调用
# ═══════════════════════════════════════════════════════


async def _parse_with_llm(content: str, plugin_dir_name: str, provider) -> Optional[PluginInfo]:
    try:
        resp = await provider.text_chat(
            prompt=_build_prompt(content),
            session_id="command_displayer_scan",
        )

        resp_str = _extract_response_text(resp)
        if not resp_str:
            logger.debug(f"LLM 返回空内容: {plugin_dir_name}")
            return None

        data = _parse_json_response(resp_str)
        if not data:
            logger.debug(f"LLM 未返回有效 JSON: {plugin_dir_name}")
            return None

        return _normalize_result(data, plugin_dir_name)

    except Exception:
        logger.debug(f"LLM 解析异常 {plugin_dir_name}")
        return None


def _build_prompt(content: str) -> str:
    return (
        "你是一个 AstrBot 插件信息提取助手。请从以下 README 文本中精确提取插件信息。\n"
        "\n"
        "【提取要求】\n"
        "1. plugin_name: 插件名称（通常在 README 标题或开头，取最精简的名称）\n"
        "2. description: 插件功能描述（一句话概括，不超过100字）\n"
        "3. commands: 所有用户可触发的命令列表，每条包含：\n"
        "   - command: 命令本身（如 /天气、/help）\n"
        "   - args: 参数说明（如 [城市名]，无参数则留空）\n"
        "   - description: 命令功能说明\n"
        "\n"
        "【注意事项】\n"
        "- 只提取用户在聊天中可直接触发的命令（通常以 / 开头）\n"
        "- 不要提取内部函数名、API 端点、开发相关命令\n"
        "- 如果 README 中有命令表格，请逐行提取\n"
        "- 如果 README 中只有功能描述没有明确命令，commands 留空数组\n"
        "- 命令必须以 / 开头，如果不带 / 请补上\n"
        "- 别名(aliases)也作为独立命令条目列出\n"
        "\n"
        "【输出格式】\n"
        "只输出合法 JSON，不要任何解释、不要 markdown 代码块。\n"
        "\n"
        "示例输出：\n"
        '{"plugin_name":"天气查询","description":"查询城市天气信息","commands":['
        '{"command":"/天气","args":"[城市名]","description":"查询指定城市天气"},'
        '{"command":"/weather","args":"[城市名]","description":"查询天气(英文别名)"}'
        ']}\n'
        "\n"
        "无命令时：\n"
        '{"plugin_name":"示例插件","description":"这是一个示例","commands":[]}\n'
        "\n"
        f"【README 内容】\n{content}"
    )


# ═══════════════════════════════════════════════════════
# 响应解析
# ═══════════════════════════════════════════════════════


def _extract_response_text(resp) -> Optional[str]:
    if not resp:
        return None

    for attr_chain in ("result_chain",):
        if hasattr(resp, attr_chain):
            chain = getattr(getattr(resp, attr_chain), "chain", None)
            if chain:
                text = "".join(getattr(c, "text", "") for c in chain)
                if text.strip():
                    return text.strip()

    for attr in ("result", "content"):
        if hasattr(resp, attr):
            text = str(getattr(resp, attr)).strip()
            if text:
                return text

    text = str(resp).strip()
    return text if text else None


def _parse_json_response(resp_str: str) -> Optional[Dict]:
    """多策略 JSON 提取"""
    # 直接解析
    data = _try_parse_json(resp_str)
    if data:
        return data

    # 去代码围栏
    data = _try_parse_json(_strip_code_fences(resp_str))
    if data:
        return data

    # 正则提取 {...} 块
    match = re.search(r"\{[\s\S]*\}", resp_str)
    if match:
        data = _try_parse_json(match.group(0))
        if data:
            return data

    # 逐行扫描
    for i, line in enumerate(resp_str.split("\n")):
        if line.strip().startswith("{"):
            data = _try_parse_json("\n".join(resp_str.split("\n")[i:]))
            if data:
                return data

    return None


def _try_parse_json(text: str) -> Optional[Dict]:
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ═══════════════════════════════════════════════════════
# 结果规范化
# ═══════════════════════════════════════════════════════


def _normalize_result(data: Dict, fallback_name: str) -> Optional[PluginInfo]:
    plugin_name = data.get("plugin_name", "").strip() or fallback_name
    description = data.get("description", "").strip()

    raw_commands = data.get("commands")
    if not isinstance(raw_commands, list):
        raw_commands = []

    commands: List[CommandEntry] = []
    seen = set()

    for cmd in raw_commands:
        if not isinstance(cmd, dict):
            continue

        command_str = str(cmd.get("command", "")).strip()
        if not command_str:
            continue

        if not command_str.startswith("/"):
            command_str = "/" + command_str

        if command_str in seen:
            continue
        seen.add(command_str)

        commands.append(CommandEntry(
            command=command_str,
            args=str(cmd.get("args", "")).strip(),
            description=str(cmd.get("description", "")).strip() or "无描述",
            source=SOURCE_LLM,
        ))

    return PluginInfo(
        name=plugin_name,
        description=description,
        commands=commands,
        source=SOURCE_LLM,
    )
