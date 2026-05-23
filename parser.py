"""LLM 解析 README：提取插件名称、描述和命令列表（含参数描述）"""

import json
import re
from typing import Dict, List, Optional

from astrbot.api import logger

from .models import CommandEntry, PluginInfo, SOURCE_LLM


async def parse_readme(
    content: str, plugin_dir_name: str, provider=None
) -> Optional[PluginInfo]:
    """使用 LLM 解析 README，提取插件名称、描述和命令列表"""
    if not provider:
        logger.error(f"未配置 LLM 提供商，无法解析 {plugin_dir_name}")
        return None

    cleaned = _preprocess_readme(content)
    if not cleaned:
        logger.debug(f"README 预处理后为空: {plugin_dir_name}")
        return None

    return await _parse_with_llm(cleaned, plugin_dir_name, provider)


# ── README 预处理 ──────────────────────────────────

# 无关章节标题（命中后截断）
_CUTOFF_PATTERNS = [
    r"^#{1,3}\s*(?:变更日志|更新日志|Changelog|CHANGELOG|更新记录)",
    r"^#{1,3}\s*(?:License|许可证|LICENSE)",
    r"^#{1,3}\s*(?:Contributing|贡献指南|CONTRIBUTING)",
    r"^#{1,3}\s*(?:Star History|Stars History)",
    r"^#{1,3}\s*(?:致谢|Acknowledgement|Credits|Thanks)",
    r"^#{1,3}\s*(?:开发|Development|Developer)",
]


def _preprocess_readme(content: str) -> str:
    """清理 README：去除无关内容，保留命令相关部分"""
    if not content:
        return ""

    text = content
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"^\s*\[!\[.*?\]\(.*?\)\]\(.*?\)\s*$", "", text, flags=re.MULTILINE)

    for pattern in _CUTOFF_PATTERNS:
        match = re.search(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
        if match:
            text = text[:match.start()]

    text = re.sub(r"```[\s\S]*?```", lambda m: m.group(0) if len(m.group(0)) < 500 else "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ── LLM 调用 ──────────────────────────────────────

_PROMPT_TEMPLATE = (
    "你是一个 AstrBot 插件信息提取助手。请从以下 README 中精确提取插件信息。\n"
    "\n"
    "【提取要求】\n"
    "1. plugin_name: 插件名称（取最精简的中文名称，不要带版本号或特殊符号）\n"
    "2. description: 插件功能描述（20-100字，概括插件的核心功能和用途，要具体而非笼统）\n"
    "3. commands: 所有用户可触发的命令列表，每条包含：\n"
    "   - command: 命令本身（如 /天气、/help，必须以 / 开头）\n"
    "   - args: 参数说明，格式如 [城市名] [日期] 或 [-s] [-d] [-t]，无参数则留空字符串\n"
    "   - args_description: 每个参数的功能说明，用分号分隔，如：城市名: 要查询的城市，必填; 日期: 查询日期，默认为今天\n"
    "   - description: 命令功能说明（15-80字，具体说明该命令做什么、怎么用）\n"
    "\n"
    "【描述质量要求】\n"
    "- 插件描述要具体：说明插件提供什么功能、解决什么问题，不要写\"这是一个xxx插件\"\n"
    "- 命令描述要包含动作和对象：如\"查询指定城市的实时天气信息\"而非\"查看天气\"\n"
    "- 如果 README 中有命令的详细用法说明，请提炼到 description 中\n"
    "- 避免\"无描述\"或空描述，即使 README 信息有限也要根据命令名合理推断\n"
    "\n"
    "【参数识别规则】\n"
    "- 必填参数：用 [参数名] 表示，如 [城市名]、[插件名]\n"
    "- 可选参数：用 [参数名] 表示，在 args_description 中标注默认值\n"
    "- 布尔 flag：用 [-字母] 表示，如 -s（简洁模式）、-d（详细模式）、-t（表格模式）、-v（verbose）\n"
    "- 混合参数：如 [subcmd] [-s|-d|-t]，表示一个必填子命令加可选格式 flag\n"
    "- 如果 README 中参数以 --option 形式出现（如 --help），也提取为 [-option]\n"
    "- 无参数的命令，args 和 args_description 都留空字符串\n"
    "\n"
    "【注意事项】\n"
    "- 只提取用户在聊天中可直接触发的命令（通常以 / 开头）\n"
    "- 不要提取内部函数名、API 端点、开发相关命令、配置项\n"
    "- 命令必须以 / 开头，如果不带 / 请补上\n"
    "- 别名(aliases)也作为独立命令条目列出\n"
    "- 如果 README 中只有功能描述没有明确命令，commands 留空数组\n"
    "- 正则过滤器命令用 正则:模式 格式表示\n"
    "\n"
    "【输出格式】\n"
    "只输出合法 JSON，不要任何解释、不要 markdown 代码块。\n"
    "\n"
    "示例（含多种参数格式和高质量描述）：\n"
    '{{"plugin_name":"天气查询","description":"提供城市天气查询服务，支持实时天气、未来预报和多城市对比",'
    '"commands":['
    '{{"command":"/天气","args":"[城市名]","args_description":"城市名: 要查询的城市名称，必填，如：北京、上海","description":"查询指定城市的实时天气信息，包括温度、湿度、风力等"}},'
    '{{"command":"/forecast","args":"[城市名] [天数]","args_description":"城市名: 要查询的城市，必填; 天数: 预报天数，默认3天，最大7天","description":"查询指定城市未来几天的天气预报趋势"}},'
    '{{"command":"/weather","args":"[-s] [-d] [-t]","args_description":"-s: 简洁模式，只显示温度; -d: 详细模式，显示全部信息，默认; -t: 表格模式","description":"查看天气信息，支持多种输出格式参数"}},'
    '{{"command":"/help","args":"","args_description":"","description":"显示天气插件的帮助信息，包含所有可用命令的说明"}}'
    ']}}\n'
    "\n"
    "无命令时：\n"
    '{{"plugin_name":"示例插件","description":"这是一个功能示例插件，用于演示插件开发规范","commands":[]}}\n'
    "\n"
    "【README 内容】\n{content}"
)


async def _parse_with_llm(
    content: str, plugin_dir_name: str, provider
) -> Optional[PluginInfo]:
    """调用 LLM 解析 README 内容"""
    try:
        resp = await provider.text_chat(
            prompt=_PROMPT_TEMPLATE.format(content=content),
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


# ── 响应解析 ──────────────────────────────────────


def _extract_response_text(resp) -> Optional[str]:
    """从 LLMResponse 对象中提取文本内容"""
    if not resp:
        return None
    if hasattr(resp, "result_chain") and resp.result_chain:
        chain = getattr(resp.result_chain, "chain", None)
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
    """多策略 JSON 提取：直接解析 → 去代码围栏 → 正则提取"""
    for text in [resp_str, _strip_code_fences(resp_str)]:
        data = _try_parse_json(text)
        if data:
            return data

    match = re.search(r"\{[\s\S]*\}", resp_str)
    if match:
        data = _try_parse_json(match.group(0))
        if data:
            return data

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


# ── 结果规范化 ──────────────────────────────────────


def _normalize_result(data: Dict, fallback_name: str) -> Optional[PluginInfo]:
    """将 LLM 返回的 JSON 规范化为 PluginInfo"""
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
            args_description=str(cmd.get("args_description", "")).strip(),
            description=str(cmd.get("description", "")).strip() or "无描述",
            source=SOURCE_LLM,
        ))

    return PluginInfo(
        name=plugin_name,
        description=description,
        commands=commands,
        source=SOURCE_LLM,
    )
