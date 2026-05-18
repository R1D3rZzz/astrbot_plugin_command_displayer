import json
from pathlib import Path
from typing import Dict, List
from astrbot.api import logger
from .models import CACHE_FILE_PATH


class CommandCache:
    """命令缓存管理（持久化到本地 JSON）"""

    def __init__(self, cache_dir: str = CACHE_FILE_PATH):
        self._cache_file = Path(cache_dir)
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._commands: Dict[str, Dict] = {}
        self._known_plugins: List[str] = []
        self._timestamp: int = 0
        self._load()

    # ── 持久化 ──────────────────────────────────────────

    def _load(self):
        if not self._cache_file.exists():
            return
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            self._commands = data.get("commands", {})
            self._known_plugins = data.get("known_plugins", [])
            self._timestamp = data.get("timestamp", 0)
            logger.info(f"已加载命令缓存，共 {len(self._commands)} 个插件，{len(self._known_plugins)} 个已知目录")
        except Exception as e:
            logger.warning(f"加载缓存失败: {e}")

    def save(self):
        try:
            self._cache_file.write_text(
                json.dumps(
                    {
                        "commands": self._commands,
                        "known_plugins": self._known_plugins,
                        "timestamp": self._timestamp,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存缓存失败: {e}")

    # ── 访问器 ──────────────────────────────────────────

    @property
    def commands(self) -> Dict[str, Dict]:
        return dict(self._commands)

    @commands.setter
    def commands(self, data: Dict[str, Dict]):
        self._commands = data

    def update_commands(self, data: Dict[str, Dict]):
        self._commands.update(data)

    def remove_commands(self, names: List[str]):
        for name in names:
            self._commands.pop(name, None)

    @property
    def known_plugins(self) -> List[str]:
        return list(self._known_plugins)

    @known_plugins.setter
    def known_plugins(self, plugins: List[str]):
        self._known_plugins = plugins

    @property
    def timestamp(self) -> int:
        return self._timestamp

    @timestamp.setter
    def timestamp(self, ts: int):
        self._timestamp = ts
