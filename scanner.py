"""插件扫描器：直接读取注册指令 + LLM 解析 README，统一构建命令索引"""

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import (
    INDEX_FILE_PATH, MAX_README_SIZE,
    PluginInfo, IndexPlugin, IndexCommand,
    SOURCE_DIRECT, SOURCE_LLM,
    name_in_data, name_matches,
)
from .reader import read_registered_commands
from .parser import parse_readme
from .cache import CommandCache


class PluginScanner:
    """混合扫描器：优先直接读取注册指令，LLM 解析作为兜底，统一输出命令索引"""

    def __init__(
        self,
        plugins_directory: str,
        max_readme_size: int = MAX_README_SIZE,
        include_disabled: bool = False,
        enable_llm_analysis: bool = True,
    ):
        self._plugins_dir = Path(plugins_directory)
        self._max_readme_size = max_readme_size
        self._include_disabled = include_disabled
        self._enable_llm_analysis = enable_llm_analysis

    # ── 目录发现 ──────────────────────────────────────

    def get_plugin_dirs(self) -> List[Path]:
        """获取所有有效插件目录"""
        if not self._plugins_dir.exists():
            logger.warning(f"插件目录不存在: {self._plugins_dir.resolve()}")
            return []
        dirs = []
        for entry in self._plugins_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if not self._include_disabled and entry.name.startswith("_"):
                continue
            dirs.append(entry)
        return sorted(dirs, key=lambda d: d.name)

    # ── 增量检测 ──────────────────────────────────────

    def get_delta(self, cache: CommandCache) -> Tuple[List[Path], List[str]]:
        """返回 (新增目录, 已删除插件名)"""
        current_dirs = self.get_plugin_dirs()
        current_names = {d.name for d in current_dirs}
        known_names = set(cache.known_plugins)
        new_dirs = [d for d in current_dirs if d.name in (current_names - known_names)]
        removed_names = list(known_names - current_names)
        return new_dirs, removed_names

    # ── 全量扫描 ──────────────────────────────────────

    async def scan_all(
        self, cache: CommandCache, context, provider=None
    ) -> Dict[str, PluginInfo]:
        """
        全量扫描：
        1. 直接读取已注册指令
        2. 若启用了 LLM 分析，对所有有 README 的插件进行 LLM 解析，
           将 LLM 提取的描述信息合并到直接读取的数据中（补充命令描述、参数说明、插件描述）
        3. 对直接读取也未覆盖的插件，LLM 解析作为兜底完整数据
        4. 合并结果并构建命令索引
        """
        dirs = self.get_plugin_dirs()
        cache.commands = {}
        cache.known_plugins = [d.name for d in dirs]

        # 直接读取
        direct_data = read_registered_commands(context)
        logger.info(f"直接读取到 {len(direct_data)} 个已注册插件")

        all_data: Dict[str, PluginInfo] = dict(direct_data)
        await self._merge_llm_data(dirs, direct_data, all_data, provider)

        cache.commands = all_data
        cache.index = self._build_index(all_data)
        cache.timestamp = int(time.time())
        cache.save()

        logger.info(
            f"全量扫描完成: 直接 {len(direct_data)} 个插件, "
            f"共 {len(all_data)} 个插件, {sum(len(p.get('commands', [])) for p in all_data.values())} 条命令"
        )
        return all_data

    # ── 增量扫描 ──────────────────────────────────────

    async def scan_new(
        self, cache: CommandCache, context, provider=None
    ) -> Dict[str, PluginInfo]:
        """增量扫描：移除已删除插件，扫描新增插件，更新命令索引"""
        _, removed = self.get_delta(cache)
        if removed:
            cache.remove_commands(removed)
            logger.info(f"移除已删除插件: {removed}")

        direct_data = read_registered_commands(context)
        new_dirs, _ = self.get_delta(cache)
        new_data: Dict[str, PluginInfo] = dict(direct_data)

        await self._merge_llm_data(new_dirs, direct_data, new_data, provider)

        if new_data:
            cache.update_commands(new_data)

        # 重建索引
        cache.index = self._build_index(cache.commands)
        cache.known_plugins = [d.name for d in self.get_plugin_dirs()]
        cache.timestamp = int(time.time())
        cache.save()
        return new_data

    # ── LLM 合并（scan_all / scan_new 共用）──────────

    async def _merge_llm_data(
        self, dirs: List[Path], direct_data: Dict[str, PluginInfo],
        target: Dict[str, PluginInfo], provider
    ):
        """将 LLM 解析的描述合并到 target 中，或作为未覆盖插件的兜底"""
        if self._enable_llm_analysis and provider:
            logger.info("LLM 分析已启用，开始解析插件 README 以补充描述...")
            for plugin_dir in dirs:
                llm_info = await self._read_and_parse(plugin_dir, provider)
                if not llm_info:
                    continue

                # 查找对应的直接读取数据
                direct_key = None
                for dk in direct_data:
                    if dk == plugin_dir.name or name_matches(dk, plugin_dir.name):
                        direct_key = dk
                        break

                if direct_key is not None:
                    merged = self._merge_llm_into_direct(direct_data[direct_key], llm_info, plugin_dir.name)
                    target[direct_key] = merged
                else:
                    target[llm_info["name"]] = llm_info
        else:
            # 未启用 LLM：对未覆盖的插件仍尝试 LLM 兜底
            uncovered = [d for d in dirs if not name_in_data(d.name, target)]
            if uncovered:
                logger.info(f"对 {len(uncovered)} 个未注册插件尝试 LLM 解析...")
                for plugin_dir in uncovered:
                    info = await self._read_and_parse(plugin_dir, provider)
                    if info:
                        target[info["name"]] = info

    # ── 单插件扫描 ────────────────────────────────────

    async def scan_single(
        self, plugin_name: str, cache: CommandCache, context, provider=None
    ) -> Optional[PluginInfo]:
        """扫描单个插件"""
        # 优先直接读取
        direct_data = read_registered_commands(context)
        for name, info in direct_data.items():
            if name_matches(name, plugin_name) or name_matches(info.get("name", ""), plugin_name):
                cache.update_commands({name: info})
                cache.index = self._build_index(cache.commands)
                cache.save()
                logger.info(f"单插件直接读取: {name}")
                return info

        # LLM 兜底
        plugin_dir = self._find_plugin_dir(plugin_name)
        if not plugin_dir or not self._enable_llm_analysis:
            return None

        info = await self._read_and_parse(plugin_dir, provider)
        if info:
            cache.update_commands({info["name"]: info})
            known = set(cache.known_plugins)
            known.add(plugin_dir.name)
            cache.known_plugins = sorted(known)
            cache.index = self._build_index(cache.commands)
            cache.save()
            logger.info(f"单插件 LLM 解析: {info['name']}")
        return info

    # ── 命令索引构建 ──────────────────────────────────

    @staticmethod
    def _build_index(data: Dict[str, PluginInfo]) -> List[IndexPlugin]:
        """
        将内存中的插件数据转换为结构化命令索引。
        参数信息直接来自 handler_params（直接读取）或 LLM 解析（README），
        不再额外推断，保证准确性。
        最后追加本插件自身的命令。
        """
        index: List[IndexPlugin] = []
        for pname, pinfo in sorted(data.items()):
            commands: List[IndexCommand] = []
            for cmd in pinfo.get("commands", []):
                args_str = cmd.get("args", "").strip()
                # 优先使用已有的 args_description（来自 handler_params 或 LLM 解析）
                args_desc = cmd.get("args_description", "").strip()
                if not args_desc and args_str:
                    # 兜底：从 args 简单生成描述
                    args_desc = _brief_args_desc(args_str)
                commands.append(IndexCommand(
                    command=cmd.get("command", ""),
                    args=args_str,
                    args_description=args_desc,
                    description=cmd.get("description", "无描述"),
                    filter_type=cmd.get("filter_type", "command"),
                ))
            index.append(IndexPlugin(
                name=pinfo.get("name", pname),
                key=pname,
                description=pinfo.get("description", ""),
                source=pinfo.get("source", SOURCE_DIRECT),
                commands=commands,
            ))

        self_index = _self_plugin_index()
        # 动态查找本插件在缓存中的 key
        for pname, pinfo in data.items():
            if pinfo.get("name") == "Command Displayer" or "command_displayer" in pname.lower():
                self_index["key"] = pname
                break
        index.append(self_index)
        return index

    # ── 内部方法 ──────────────────────────────────────

    @staticmethod
    def _merge_llm_into_direct(
        direct: PluginInfo, llm: PluginInfo, dir_name: str
    ) -> PluginInfo:
        """
        将 LLM 解析的描述信息合并到直接读取的插件数据中。

        合并策略：
        - 插件描述：优先使用直接读取的（通常更准确），若为空则用 LLM 的
        - 命令列表：以直接读取的命令结构为基础（命令名、参数格式来自 handler_params），
          用 LLM 解析的描述信息补充每条命令的 description 和 args_description
        - 如果 LLM 发现了直接读取中缺失的命令（README 中有但 handler 未注册的），
          也追加进来（标记 source=llm）
        - source 标记为 "direct+llm" 表示已合并
        """
        merged: PluginInfo = {
            "name": direct.get("name") or llm.get("name") or dir_name,
            "description": direct.get("description") or llm.get("description", ""),
            "commands": [],
            "source": "direct+llm",
        }

        # 建立 LLM 命令的查找表（按命令名归一化）
        llm_cmd_map: Dict[str, dict] = {}
        for llm_cmd in llm.get("commands", []):
            key = llm_cmd.get("command", "").lower().lstrip("/")
            if key:
                llm_cmd_map[key] = llm_cmd

        # 处理直接读取的命令：用 LLM 描述补充
        direct_cmd_keys: set = set()
        for d_cmd in direct.get("commands", []):
            cmd_name = d_cmd.get("command", "")
            cmd_key = cmd_name.lower().lstrip("/")
            direct_cmd_keys.add(cmd_key)

            # 查找 LLM 中对应的命令描述
            llm_cmd = llm_cmd_map.get(cmd_key, {})

            merged_cmd = dict(d_cmd)
            # 补充命令描述：直接读取为空时用 LLM 的
            if not merged_cmd.get("description", "").strip() or merged_cmd.get("description") == "无描述":
                llm_desc = llm_cmd.get("description", "").strip()
                if llm_desc and llm_desc != "无描述":
                    merged_cmd["description"] = llm_desc
            # 补充参数说明：直接读取为空时用 LLM 的
            if not merged_cmd.get("args_description", "").strip():
                llm_args_desc = llm_cmd.get("args_description", "").strip()
                if llm_args_desc:
                    merged_cmd["args_description"] = llm_args_desc

            merged["commands"].append(merged_cmd)

        # 追加 LLM 中发现但直接读取中缺失的命令
        for llm_cmd in llm.get("commands", []):
            key = llm_cmd.get("command", "").lower().lstrip("/")
            if key and key not in direct_cmd_keys:
                merged["commands"].append(dict(llm_cmd))

        return merged

    def _find_plugin_dir(self, plugin_name: str) -> Optional[Path]:
        """根据名称查找插件目录"""
        # 精确匹配
        target = self._plugins_dir / plugin_name
        if target.is_dir():
            return target
        # 模糊匹配
        for d in self.get_plugin_dirs():
            if name_matches(d.name, plugin_name) or name_matches(d.name.lstrip("_"), plugin_name):
                return d
        return None

    async def _read_and_parse(
        self, plugin_dir: Path, provider=None
    ) -> Optional[PluginInfo]:
        """读取 README 并用 LLM 解析"""
        readme = plugin_dir / "README.md"
        if not readme.exists():
            readme = plugin_dir / "readme.md"
        if not readme.exists():
            return None
        try:
            if readme.stat().st_size > self._max_readme_size:
                logger.debug(f"跳过过大 README: {plugin_dir.name}")
                return None
            content = readme.read_text(encoding="utf-8")
            info = await parse_readme(content, plugin_dir.name, provider)
            if info:
                info["source"] = SOURCE_LLM
                for cmd in info.get("commands", []):
                    cmd["source"] = SOURCE_LLM
            return info
        except Exception as e:
            logger.error(f"解析 {plugin_dir.name} README 失败: {e}")
            return None


def _self_plugin_index() -> IndexPlugin:
    """
    构建本插件自身的命令索引，追加到 command_index.json 中。
    参数定义与 main.py 中 @filter.command 装饰的 handler 签名保持一致。
    """
    return IndexPlugin(
        name="Command Displayer",
        description="AstrBot 插件命令中枢，统一管理和查询所有插件命令",
        source=SOURCE_DIRECT,
        commands=[
            # /LLM [自然语言]
            IndexCommand(
                command="/LLM",
                args="[query]",
                args_description="query: 自然语言描述，如：帮我查北京天气、查看所有命令",
                description="用自然语言描述意图，AI 匹配最相关的具体命令并执行",
                filter_type="command",
            ),
            # /命令 [subcmd] [arg]  (main.py: command_handler(self, event, subcmd="", arg=""))
            IndexCommand(
                command="/命令",
                args="[subcmd] [-s|-d|-t]",
                args_description=(
                    "subcmd: all/全部 查看所有插件命令；插件名 查看指定插件命令；"
                    "delete 插件名/all 删除记录；"
                    "-s: 简洁模式（只显示命令名）；"
                    "-d: 详细模式（显示命令名和描述，默认）；"
                    "-t: 表格模式"
                ),
                description="查看插件的命令列表及其描述，支持按插件名筛选、按格式输出，也可删除缓存记录",
                filter_type="command",
            ),
            # /扫描 [subcmd]  (main.py: scan_handler(self, event, subcmd=""))
            IndexCommand(
                command="/扫描",
                args="[subcmd]",
                args_description=(
                    "subcmd: all/全部 全量扫描；add/增量 增量扫描；插件名 扫描单个插件"
                ),
                description="扫描插件目录，刷新命令缓存和索引（首次使用前需先执行全量扫描）",
                filter_type="command",
            ),
            # /全部插件  (无参数)
            IndexCommand(
                command="/全部插件",
                args="",
                args_description="无参数",
                description="列出所有已安装插件的名称和数量概览，不含各插件的命令详情",
                filter_type="command",
            ),
            # /帮助  (无参数)
            IndexCommand(
                command="/帮助",
                args="",
                args_description="无参数",
                description="显示本插件(Command Displayer)的命令帮助，仅含本插件用法，不含其他插件",
                filter_type="command",
            ),
        ],
    )


def _brief_args_desc(args_str: str) -> str:
    """从参数字符串生成简要描述（兜底用，优先使用 handler_params 或 LLM 解析的结果）"""
    if not args_str:
        return "无参数"
    # 去掉方括号，用分号连接各参数名
    parts = [p.strip().strip("[]") for p in args_str.split() if p.strip()]
    if not parts:
        return args_str
    return "; ".join(f"{p}: 参数" for p in parts if p)
