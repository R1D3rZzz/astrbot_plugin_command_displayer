"""Command Displayer — AstrBot 插件命令中枢

核心功能：
1. 扫描所有插件命令（直接读取 + LLM 解析 README 兜底）
2. 生成结构化命令索引（command_index.json），包含插件/命令/参数描述
3. LLM 命令级路由：自然语言 → 具体命令，支持 auto/confirm 执行模式
"""

import asyncio
import time
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

from .cache import CommandCache
from .formatter import format_all, format_plugin, format_plugin_list
from .models import LOG_LEVEL_MAP
from .router import execute_command, llm_resolve, llm_resolve_all
from .scanner import PluginScanner


class CommandDisplayer(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = config or {}

        # ── 配置 ──────────────────────────────────
        self.plugin_scan_interval = cfg.get("plugin_scan_interval", 300)
        self.cache_timeout = cfg.get("cache_timeout", 30) * 60
        self.max_commands_per_plugin = cfg.get("max_commands_per_plugin", 200)
        self.enable_auto_reload = cfg.get("enable_auto_reload", True)
        self.command_format = cfg.get("command_format", "detailed")
        self.llm_execute_mode = cfg.get("llm_execute_mode", "confirm")
        self.llm_full_proxy = cfg.get("llm_full_proxy", False)

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

        # ── LLM 路由待确认状态 ─────────────────────
        self._pending_llm: dict = {}  # sender_id → {plugin_name, command_name, args, timestamp}

        logger.info(
            f"Command Displayer 初始化完成（格式={self.command_format}, "
            f"扫描={self.enable_auto_reload}, "
            f"执行={self.llm_execute_mode}, LLM分析={cfg.get('enable_llm_analysis', True)}）"
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
        self._pending_llm.clear()
        logger.info("Command Displayer 插件已卸载")

    # ── 插件名解析（/命令 [插件名] 用） ──────────────

    async def _resolve_plugin(
        self, query: str, data: dict, provider
    ) -> Optional[str]:
        """
        使用 LLM 解析插件名（仅用于 /命令 [插件名] 场景）。
        """
        if not data or not query:
            return None
        if query in data:
            return query

        result = await llm_resolve(query, data, provider)
        if result:
            pname, _, _, _ = result
            return pname if pname != "__list_all__" else None
        return None

    # ── /帮助 ──────────────────────────────────────

    @filter.command("帮助")
    async def help_handler(self, event: AstrMessageEvent):
        """查看插件命令帮助 — 显示所有可用命令的用法说明和示例  """
        text = (
            "Command Displayer 命令帮助\n"
            "\n"
            "/LLM [自然语言]             - 用自然语言描述你想做什么，AI 匹配具体命令并执行\n"
            "  示例: /LLM 查看天气的命令\n"
            "  示例: /LLM 帮我查一下北京天气\n"
            "\n"
            "/命令 [子命令] [参数] [格式]\n"
            "  /命令 all / 全部          - 查看所有插件命令\n"
            "  /命令 [插件名]            - 查看指定插件命令\n"
            "  /命令 delete all / 全部   - 删除全部记录\n"
            "  /命令 delete [插件名]     - 删除指定插件记录\n"
            "\n"
            "格式参数（可选）\n"
            "  -s 简洁模式  -d 详细模式  -t 表格模式\n"
            "  示例: /命令 all -t\n"
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
        """扫描插件命令 — 扫描插件目录获取命令信息。支持全量扫描、增量扫描、扫描单个插件 """
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
                yield event.plain_result(
                    f"[OK] 全量扫描完成，共加载 {len(self.cache.commands)} 个插件的命令"
                )
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
        """显示已经安装的全部插件 — 列出所有已加载插件的名称、数据来源、命令数量和描述   """
        data = self.cache.commands
        if not data:
            yield event.plain_result("[X] 未找到任何插件（请先使用 /扫描）")
            return
        yield event.plain_result(format_plugin_list(data))

    # ── /LLM ───────────────────────────────────────

    @filter.command("LLM")
    async def llm_handler(self, event: AstrMessageEvent, subcmd: str = ""):
        """LLM 命令级路由：匹配到具体命令，支持自动执行或确认后执行 — 用自然语言描述意图，AI 自动匹配最相关的具体命令并可选执行 """
        if not subcmd:
            mode_desc = "全权代理" if self.llm_full_proxy else "标准路由"
            yield event.plain_result(
                f"/LLM [自然语言]  （当前模式: {mode_desc}）\n"
                "  示例: /LLM 查看天气的命令\n"
                "  示例: /LLM 帮我查一下北京天气\n"
                "\n"
                "AI 会根据你的描述匹配最相关的命令。"
            )
            return

        data = self.cache.commands
        if not data:
            yield event.plain_result("[X] 未找到任何插件（请先使用 /扫描）")
            return

        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("[X] 当前没有可用的 LLM 提供商")
            return

        # ── 全权代理模式 ──
        if self.llm_full_proxy:
            await self._llm_full_proxy_handle(event, subcmd, data, provider)
            return

        # ── 标准路由模式 ──
        result = await llm_resolve(subcmd, data, provider)
        if not result:
            yield event.plain_result(
                f"[X] 无法理解你的意图：`{subcmd}`\n"
                "请尝试更具体的描述，或使用 /全部插件 查看可用插件。"
            )
            return

        plugin_name, command_name, args_str, confirm_msg = result
        await self._handle_route_result(
            event, data, plugin_name, command_name, args_str, confirm_msg
        )

    async def _llm_full_proxy_handle(
        self, event: AstrMessageEvent, subcmd: str, data: dict, provider
    ):
        """处理全权代理模式的 LLM 路由"""
        result = await llm_resolve_all(subcmd, data, provider)
        if not result:
            yield event.plain_result(
                f"[X] 无法理解你的意图：`{subcmd}`\n"
                "请尝试更具体的描述，或使用 /全部插件 查看可用插件。"
            )
            return

        action, plugin_name, command_name, args_str, msg = result

        if action == "NONE":
            yield event.plain_result(f"❌ {msg}")
            return

        if action == "LIST_ALL":
            yield event.plain_result(
                format_all(data, self.max_commands_per_plugin, self.command_format)
            )
            return

        if action == "SHOW":
            pinfo = data.get(plugin_name)
            display = pinfo.get("name", plugin_name) if pinfo else plugin_name
            if pinfo:
                yield event.plain_result(
                    format_plugin(plugin_name, pinfo, self.max_commands_per_plugin, self.command_format)
                )
            else:
                yield event.plain_result(f"⚠️ {msg}\n（插件缓存中未找到，请先 /扫描）")
            return

        if action == "EXEC":
            if command_name:
                cmd_trigger = command_name.lstrip("/")
                exec_cmd = f"{cmd_trigger} {args_str}".strip()
                logger.info(f"LLM 全权代理执行: /{exec_cmd}")
                success = execute_command(event, cmd_trigger, args_str, self.context)
                if success:
                    yield event.plain_result(f"✅ {msg}\n正在执行 `/{exec_cmd}`...")
                else:
                    yield event.plain_result(f"⚠️ {msg}\n执行失败，请手动发送：`/{exec_cmd}`")
            else:
                # 没有具体命令，降级为 SHOW
                pinfo = data.get(plugin_name)
                if pinfo:
                    yield event.plain_result(
                        format_plugin(plugin_name, pinfo, self.max_commands_per_plugin, self.command_format)
                    )
                else:
                    yield event.plain_result(f"⚠️ {msg}")
            return

    async def _handle_route_result(
        self,
        event: AstrMessageEvent,
        data: dict,
        plugin_name: str,
        command_name: str,
        args_str: str,
        confirm_msg: str,
    ):
        """处理标准路由模式的结果（auto/confirm 执行逻辑）"""
        # 特殊：用户想看全部
        if plugin_name == "__list_all__":
            yield event.plain_result(
                format_all(data, self.max_commands_per_plugin, self.command_format)
            )
            return

        pinfo = data.get(plugin_name)

        # 构建展示信息
        display = pinfo.get("name", plugin_name) or plugin_name if pinfo else plugin_name
        if command_name and command_name != "-":
            cmd_show = command_name
            if not cmd_show.startswith("/") and not cmd_show.startswith("正则:") and not cmd_show.startswith("["):
                cmd_show = "/" + cmd_show
            if args_str:
                cmd_show += f" {args_str}"
            detail = f"匹配到 **{display}** 的命令：`{cmd_show}`"
        else:
            detail = f"匹配到插件 **{display}**"

        # ── auto 模式：直接执行 ──
        if self.llm_execute_mode == "auto":
            if command_name and command_name != "-":
                cmd_trigger = command_name.lstrip("/")
                exec_cmd = f"{cmd_trigger} {args_str}".strip()

                logger.info(f"LLM auto 执行: /{exec_cmd}")
                success = execute_command(event, cmd_trigger, args_str, self.context)

                if success:
                    yield event.plain_result(f"✅ {detail}\n正在执行 `/{exec_cmd}`...")
                else:
                    yield event.plain_result(f"⚠️ {detail}\n执行失败，请手动发送：`/{exec_cmd}`")
            elif pinfo:
                yield event.plain_result(
                    format_plugin(plugin_name, pinfo, self.max_commands_per_plugin, self.command_format)
                )
            else:
                yield event.plain_result(f"⚠️ {detail}\n（插件缓存中未找到，请先 /扫描）")
            return

        # ── confirm 模式：先确认 ──
        pending_key = event.get_sender_id() or event.get_session_id()
        self._pending_llm[pending_key] = {
            "plugin_name": plugin_name,
            "command_name": command_name,
            "args": args_str,
            "timestamp": time.time(),
        }

        if command_name and command_name != "-":
            cmd_show = command_name
            if not cmd_show.startswith("/") and not cmd_show.startswith("正则:") and not cmd_show.startswith("["):
                cmd_show = "/" + cmd_show
            if args_str:
                cmd_show += f" {args_str}"

            yield event.plain_result(
                f"{detail}\n"
                f"确认要执行 `{cmd_show}` 吗？\n"
                f"请回复 **确认** 或 **是** 来执行，回复其他内容取消。\n"
                f"（60 秒后过期）"
            )
        elif pinfo:
            yield event.plain_result(
                format_plugin(plugin_name, pinfo, self.max_commands_per_plugin, self.command_format)
            )
        else:
            yield event.plain_result(f"⚠️ {detail}\n（插件缓存中未找到，请先 /扫描）")

    # ── LLM 确认监听器 ─────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def llm_pending_listener(self, event: AstrMessageEvent):
        """监听所有消息，处理 LLM 路由的确认/取消"""
        if not self._pending_llm:
            return

        sender_id = event.get_sender_id() or event.get_session_id()
        if not sender_id:
            return

        pending = self._pending_llm.get(sender_id)
        if not pending:
            return

        # 60 秒过期
        if time.time() - pending["timestamp"] > 60:
            del self._pending_llm[sender_id]
            return

        resp = (event.get_message_str() if hasattr(event, "get_message_str") else "").strip().lower()
        if resp not in ("确认", "是", "yes", "y", "ok", "好", "是的", "取消", "不", "no", "n", "算了"):
            return

        del self._pending_llm[sender_id]

        if resp in ("确认", "是", "yes", "y", "ok", "好", "是的"):
            plugin_name = pending["plugin_name"]
            command_name = pending["command_name"]
            args_str = pending.get("args", "")

            if command_name and command_name != "-":
                cmd_trigger = command_name.lstrip("/")
                exec_cmd = f"{cmd_trigger} {args_str}".strip()

                logger.info(f"LLM confirm 执行: /{exec_cmd}")
                success = execute_command(event, cmd_trigger, args_str, self.context)

                if success:
                    yield event.plain_result(f"✅ 正在执行 `/{exec_cmd}`...")
                else:
                    yield event.plain_result(f"⚠️ 执行失败，请手动发送：`/{exec_cmd}`")
            else:
                pinfo = self.cache.commands.get(plugin_name)
                if pinfo:
                    yield event.plain_result(
                        format_plugin(plugin_name, pinfo, self.max_commands_per_plugin, self.command_format)
                    )
                else:
                    yield event.plain_result(f"[X] 插件 `{plugin_name}` 已不存在")
        else:
            yield event.plain_result("已取消。")

        event.stop_event()

    # ── /命令 ──────────────────────────────────────

    @filter.command("命令")
    async def command_handler(self, event: AstrMessageEvent, subcmd: str = "", arg: str = ""):
        """查看插件的命令 — 查询已缓存的插件命令信息。支持查看全部、查看指定插件、删除记录，以及 -s/-d/-t 三种输出格式"""
        if not subcmd:
            yield event.plain_result(
                "/命令 用法：\n"
                "  /命令 all / 全部 [-s|-d|-t]     - 查看所有插件命令\n"
                "  /命令 [插件名] [-s|-d|-t]        - 查看指定插件命令\n"
                "  /命令 delete all / 全部          - 删除全部记录\n"
                "  /命令 delete [插件名]            - 删除指定插件记录\n"
                "\n"
                "格式参数：-s 简洁  -d 详细  -t 表格\n"
                "\n"
                "/LLM [自然语言]                    - AI 模糊匹配并执行命令"
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
        fmt = self.command_format
        if "-s" in flags:
            fmt = "simple"
        elif "-t" in flags:
            fmt = "table"
        elif "-d" in flags:
            fmt = "detailed"

        data = self.cache.commands

        if not positional:
            yield event.plain_result(
                "/命令 用法：\n"
                "  /命令 all / 全部 [-s|-d|-t]     - 查看所有插件命令\n"
                "  /命令 [插件名] [-s|-d|-t]        - 查看指定插件命令\n"
                "  /命令 delete all / 全部          - 删除全部记录\n"
                "  /命令 delete [插件名]            - 删除指定插件记录\n"
                "\n"
                "/LLM [自然语言]                    - AI 模糊匹配并执行命令"
            )
            return

        action = positional[0].lower()

        # 删除
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

            provider = self.context.get_using_provider()
            matched = await self._resolve_plugin(target, data, provider)
            if not matched:
                yield event.plain_result(f"[X] 未找到插件 `{target}`\n使用 /全部插件 查看所有可用插件")
                return
            self.cache.remove_commands([matched])
            self.cache.save()
            yield event.plain_result(f"[-] 已删除插件 `{matched}` 的记录")
            return

        # 查看全部
        if action in ("all", "全部"):
            if not data:
                yield event.plain_result("[X] 未找到任何插件命令（请先使用 /扫描）")
                return
            yield event.plain_result(format_all(data, self.max_commands_per_plugin, fmt))
            return

        # 查看指定插件
        if not data:
            yield event.plain_result("[X) 未找到任何插件命令（请先使用 /扫描）")
            return

        plugin_name = positional[0]
        provider = self.context.get_using_provider()
        matched = await self._resolve_plugin(plugin_name, data, provider)
        if not matched:
            yield event.plain_result(f"[X] 未找到插件 `{plugin_name}`\n使用 /全部插件 查看所有可用插件")
            return

        yield event.plain_result(
            format_plugin(matched, data[matched], self.max_commands_per_plugin, fmt)
        )
