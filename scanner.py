import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from astrbot.api import logger

from .models import PluginInfo, SOURCE_DIRECT, SOURCE_LLM, name_in_data, name_matches
from .reader import read_registered_commands
from .parser import parse_readme
from .cache import CommandCache


class PluginScanner:
    """混合扫描器：优先直接读取注册指令，LLM 解析作为兜底"""

    def __init__(
        self,
        plugins_directory: str,
        max_readme_size: int = 1048576,
        include_disabled: bool = False,
        enable_llm_analysis: bool = True,
    ):
        self._plugins_dir = Path(plugins_directory)
        self._max_readme_size = max_readme_size
        self._include_disabled = include_disabled
        self._enable_llm_analysis = enable_llm_analysis

    # ── 目录发现 ───────────────────────────────────────

    def get_plugin_dirs(self) -> List[Path]:
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

    # ── 增量检测 ───────────────────────────────────────

    def get_delta(self, cache: CommandCache) -> Tuple[List[Path], List[str]]:
        current_dirs = self.get_plugin_dirs()
        current_names = {d.name for d in current_dirs}
        known_names = set(cache.known_plugins)
        new_dirs = [d for d in current_dirs if d.name in (current_names - known_names)]
        removed_names = list(known_names - current_names)
        return new_dirs, removed_names

    # ── 全量扫描 ───────────────────────────────────────

    async def scan_all(self, cache: CommandCache, context, provider=None) -> Dict[str, PluginInfo]:
        dirs = self.get_plugin_dirs()
        cache.commands = {}
        cache.known_plugins = [d.name for d in dirs]

        # 1. 直接读取已注册指令
        direct_data = read_registered_commands(context)
        logger.info(f"直接读取到 {len(direct_data)} 个已注册插件的指令")

        # 2. 对目录中未覆盖的插件尝试 LLM
        llm_data: Dict[str, PluginInfo] = {}
        uncovered = [d for d in dirs if not name_in_data(d.name, direct_data)]

        if self._enable_llm_analysis and uncovered:
            logger.info(f"对 {len(uncovered)} 个未注册插件尝试 LLM 解析...")
            for plugin_dir in uncovered:
                info = await self._read_and_parse(plugin_dir, provider)
                if info:
                    llm_data[info["name"]] = info

        # 3. 合并
        all_data = {**direct_data, **llm_data}
        cache.commands = all_data
        cache.timestamp = int(time.time())
        cache.save()

        logger.info(f"全量扫描完成：直接读取 {len(direct_data)} + LLM解析 {len(llm_data)}，共 {len(all_data)} 个插件")
        return all_data

    # ── 增量扫描 ───────────────────────────────────────

    async def scan_new(self, cache: CommandCache, context, provider=None) -> Dict[str, PluginInfo]:
        _, removed = self.get_delta(cache)
        if removed:
            cache.remove_commands(removed)
            logger.info(f"从缓存中移除已删除的插件: {removed}")

        # 重新读取已注册指令
        direct_data = read_registered_commands(context)

        # 对新增目录尝试 LLM
        new_dirs, _ = self.get_delta(cache)
        llm_data: Dict[str, PluginInfo] = {}
        if self._enable_llm_analysis and new_dirs:
            for plugin_dir in new_dirs:
                if name_in_data(plugin_dir.name, direct_data):
                    continue
                info = await self._read_and_parse(plugin_dir, provider)
                if info:
                    llm_data[info["name"]] = info

        new_data = {**direct_data, **llm_data}
        if new_data:
            cache.update_commands(new_data)
            logger.info(f"增量扫描更新: {list(new_data.keys())}")

        cache.known_plugins = [d.name for d in self.get_plugin_dirs()]
        cache.timestamp = int(time.time())
        cache.save()
        return new_data

    # ── 单插件扫描 ─────────────────────────────────────

    async def scan_single(self, plugin_name: str, cache: CommandCache, context, provider=None) -> Optional[PluginInfo]:
        # 优先直接读取
        direct_data = read_registered_commands(context)
        for name, info in direct_data.items():
            if name_matches(name, plugin_name) or name_matches(info.get("name", ""), plugin_name):
                cache.update_commands({name: info})
                cache.save()
                logger.info(f"单插件直接读取完成: {name}")
                return info

        # 回退到 LLM
        plugin_dir = self._plugins_dir / plugin_name
        if not plugin_dir.is_dir():
            plugin_dir = self._plugins_dir / plugin_name.lstrip("/").split("/")[-1]
        if not plugin_dir.is_dir():
            return None

        if not self._enable_llm_analysis:
            return None

        info = await self._read_and_parse(plugin_dir, provider)
        if info:
            cache.update_commands({info["name"]: info})
            known = set(cache.known_plugins)
            known.add(plugin_dir.name)
            cache.known_plugins = sorted(known)
            cache.save()
            logger.info(f"单插件 LLM 解析完成: {info['name']}")
        return info

    # ── 内部 ───────────────────────────────────────────

    async def _read_and_parse(self, plugin_dir: Path, provider=None) -> Optional[PluginInfo]:
        """读取 README 并 LLM 解析，统一标记 source"""
        readme = plugin_dir / "README.md"
        if not readme.exists():
            readme = plugin_dir / "readme.md"
        if not readme.exists():
            return None

        try:
            if readme.stat().st_size > self._max_readme_size:
                logger.debug(f"跳过过大 README: {plugin_dir.name} ({readme.stat().st_size} bytes)")
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
