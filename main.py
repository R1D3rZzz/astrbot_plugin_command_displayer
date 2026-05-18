import asyncio
import time
from typing import Optional
from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

from .scanner import PluginScanner
from .cache import CommandCache
from .formatter import format_all, format_plugin, format_plugin_list, fuzzy_find
from .models import LOG_LEVEL_MAP


class CommandDisplayer(Star):
    """AstrBot 插件命令中枢 — 直接读取注册指令 + LLM 兜底"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = config or {}

        # ── 配置 ─────────────────────────────────────
        self.plugin_scan_interval = cfg.get("plugin_scan_interval", 300)
        self.cache_timeout = cfg.get("cache_timeout", 30) * 60
        self.max_commands_per_plugin = cfg.get("max_commands_per_plugin", 200)
        self.enable_auto_reload = cfg.get("enable_auto_reload", True)
        self.command_format = cfg.get("command_format", "detailed")
        self.fuzzy_search_threshold = cfg.get("fuzzy_search_threshold", 0.6)

        log_level = cfg.get("log_level", "INFO")
        if hasattr(logger, "setLevel"):
            logger.setLevel(LOG_LEVEL_MAP.get(log_level, 20))

        # ── 子模块 ──────────────────────────────────
        self.cache = CommandCache()
        self.scanner = PluginScanner(
            plugins_directory=cfg.get("plugins_directory", "/AstrBot/data/plugins"),
            max_readme_size=cfg.get("max_readme_size", 1048576),
            include_disabled=cfg.get("include_disabled_plugins", False),
            enable_llm_analysis=cfg.get("enable_llm_analysis", True),
        )

        # ── 后台定时扫描 ───────────────────────────
        self._scan_task: Optional[asyncio.Task] = None
        if self.enable_auto_reload:
            self._start_background_scan()

        logger.info(
            f"Command Displayer 初始化完成（格式={self.command_format}, "
            f"自动扫描={self.enable_auto_reload}, "
            f"LLM分析={cfg.get('enable_llm_analysis', True)}）"
        )

    # ── 生命周期 ──────────────────────────────────

    def _start_background_scan(self):
        async def scan_loop():
            while True:
                try:
                    provider = self.context.get_using_provider()
                    now = time.time()
                    if self.cache.timestamp == 0 or (now - self.cache.timestamp > self.cache_timeout):
                        logger.info("缓存已过期，执行全量扫描...")
                        await self.scanner.scan_all(self.cache, self.context, provider)
                    else:
                        new_data = await self.scanner.scan_new(self.cache, self.context, provider)
                        if new_data:
                            logger.info(f"定时扫描发现新插件: {list(new_data.keys())}")
                    await asyncio.sleep(self.plugin_scan_interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"后台扫描异常: {e}")
                    await asyncio.sleep(60)

        self._scan_task = asyncio.create_task(scan_loop())

    async def terminate(self):
        if self._scan_task:
            self._scan_task.cancel()
        logger.info("Command Displayer 插件已卸载")

    # ── /帮助 ──────────────────────────────────────

    @filter.command("帮助")
    async def help_handler(self, event: AstrMessageEvent):
        text = (
            "Command Displayer 命令帮助\n"
            "\n"
            "/命令 [子命令] [参数] [格式]\n"
            "  /命令 all / 全部          - 查看所有插件命令\n"
            "  /命令 [插件名]            - 查看指定插件命令\n"
            "  /命令 delete all / 全部   - 删除全部记录\n"
            "  /命令 delete [插件名]     - 删除指定插件记录\n"
            "\n"
            "格式参数（可选，追加在命令末尾）\n"
            "  -s                        - 简洁模式\n"
            "  -d                        - 详细模式\n"
            "  -t                        - 表格模式\n"
            "  示例: /命令 all -t / 命令 插件名 -s\n"
            "\n"
            "/全部插件                   - 列出所有插件名称\n"
            "\n"
            "/扫描 [子命令]\n"
            "  /扫描 all / 全部          - 全量扫描所有插件\n"
            "  /扫描 [插件名]            - 扫描指定插件\n"
            "  /扫描 add / 增量          - 增量扫描新增插件"
        )
        yield event.plain_result(text)

    # ── /扫描 ──────────────────────────────────────

    @filter.command("扫描")
    async def scan_handler(self, event: AstrMessageEvent, subcmd: str = ""):
        if not subcmd:
            yield event.plain_result(
                "/扫描 用法：\n"
                "  /扫描 all / 全部   - 全量扫描\n"
                "  /扫描 [插件名]     - 扫描单个插件\n"
                "  /扫描 add / 增量   - 增量扫描"
            )
            return

        s = subcmd.lower()

        if s in ("all", "全部"):
            yield event.plain_result("[*] 正在全量扫描插件命令，请稍候...")
            try:
                provider = self.context.get_using_provider()
                await self.scanner.scan_all(self.cache, self.context, provider)
                yield event.plain_result(f"[OK] 全量扫描完成，共加载 {len(self.cache.commands)} 个插件的命令")
            except Exception as e:
                logger.error(f"全量扫描失败: {e}")
                yield event.plain_result(f"[X] 扫描失败: {e}")
            return

        if s in ("add", "增量"):
            yield event.plain_result("[*] 正在增量扫描新增插件...")
            try:
                provider = self.context.get_using_provider()
                new_data = await self.scanner.scan_new(self.cache, self.context, provider)
                if new_data:
                    yield event.plain_result(f"[OK] 增量扫描完成，发现新插件: {', '.join(new_data)}")
                else:
                    yield event.plain_result("[OK] 增量扫描完成，未发现新插件")
            except Exception as e:
                logger.error(f"增量扫描失败: {e}")
                yield event.plain_result(f"[X] 增量扫描失败: {e}")
            return

        # 单插件扫描
        yield event.plain_result(f"[*] 正在扫描插件 `{subcmd}`...")
        try:
            provider = self.context.get_using_provider()
            info = await self.scanner.scan_single(subcmd, self.cache, self.context, provider)
            if info:
                yield event.plain_result(f"[OK] 插件 `{info['name']}` 扫描完成")
            else:
                yield event.plain_result(f"[X] 未找到插件 `{subcmd}` 或其 README 解析失败")
        except Exception as e:
            logger.error(f"单插件扫描失败: {e}")
            yield event.plain_result(f"[X] 扫描失败: {e}")

    # ── /全部插件 ──────────────────────────────────

    @filter.command("全部插件")
    async def list_plugins_handler(self, event: AstrMessageEvent):
        data = self.cache.commands
        if not data:
            yield event.plain_result("[X] 未找到任何插件（请先使用 /扫描）")
            return
        yield event.plain_result(format_plugin_list(data))

    # ── /命令 ──────────────────────────────────────

    @filter.command("命令")
    async def command_handler(self, event: AstrMessageEvent, subcmd: str = "", arg: str = ""):
        if not subcmd:
            yield event.plain_result(
                "/命令 用法：\n"
                "  /命令 all / 全部 [-s|-d|-t]     - 查看所有插件命令\n"
                "  /命令 [插件名] [-s|-d|-t]        - 查看指定插件命令\n"
                "  /命令 delete all / 全部          - 删除全部记录\n"
                "  /命令 delete [插件名]            - 删除指定插件记录\n"
                "\n"
                "格式参数：\n"
                "  -s      简洁模式\n"
                "  -d      详细模式（默认）\n"
                "  -t      表格模式"
            )
            return

        # 解析参数
        tokens = [subcmd] + ([arg] if arg else [])
        flags = set()
        positional = []
        for t in tokens:
            tl = t.lower()
            if tl in ("-s", "-d", "-t"):
                flags.add(tl)
            else:
                positional.append(t)

        # 确定显示格式
        if "-s" in flags:
            fmt = "simple"
        elif "-t" in flags:
            fmt = "table"
        elif "-d" in flags:
            fmt = "detailed"
        else:
            fmt = self.command_format

        data = self.cache.commands

        # 无位置参数 → 显示帮助
        if not positional:
            yield event.plain_result(
                "/命令 用法：\n"
                "  /命令 all / 全部 [-s|-d|-t]     - 查看所有插件命令\n"
                "  /命令 [插件名] [-s|-d|-t]        - 查看指定插件命令\n"
                "  /命令 delete all / 全部          - 删除全部记录\n"
                "  /命令 delete [插件名]            - 删除指定插件记录"
            )
            return

        action = positional[0].lower()

        # ── 删除 ──
        if action == "delete":
            target = positional[1] if len(positional) > 1 else ""
            if not target:
                yield event.plain_result("[X] 请指定要删除的插件名或使用 all / 全部")
                return

            if target.lower() in ("all", "全部"):
                if not data:
                    yield event.plain_result("[X] 没有任何记录可删除")
                    return
                count = len(data)
                self.cache.commands = {}
                self.cache.save()
                yield event.plain_result(f"[-] 已删除全部 {count} 条插件记录")
                return

            matched = fuzzy_find(data, target, self.fuzzy_search_threshold)
            if not matched:
                yield event.plain_result(f"[X] 未找到插件 `{target}`")
                return
            self.cache.remove_commands([matched])
            self.cache.save()
            yield event.plain_result(f"[-] 已删除插件 `{matched}` 的记录")
            return

        # ── 查看全部 ──
        if action in ("all", "全部"):
            if not data:
                yield event.plain_result("[X] 未找到任何插件命令（请先使用 /扫描）")
                return
            yield event.plain_result(format_all(data, self.max_commands_per_plugin, fmt))
            return

        # ── 查看指定插件 ──
        if not data:
            yield event.plain_result("[X] 未找到任何插件命令（请先使用 /扫描）")
            return

        plugin_name = positional[0]
        matched = fuzzy_find(data, plugin_name, self.fuzzy_search_threshold)
        if not matched:
            yield event.plain_result(f"[X] 未找到插件 `{plugin_name}`")
            return

        yield event.plain_result(
            format_plugin(matched, data[matched], self.max_commands_per_plugin, fmt)
        )
