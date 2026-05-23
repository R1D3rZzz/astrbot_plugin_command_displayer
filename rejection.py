"""LLM 路由拒绝记录：持久化保存用户拒绝过的 (query → 命令) 映射"""

import json
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api import logger

REJECTION_FILE_PATH = "data/command_displayer/llm_rejections.json"


class RejectionStore:
    """管理用户拒绝过的 LLM 路由结果，防止重复匹配到同一错误命令"""

    def __init__(self, path: str = REJECTION_FILE_PATH):
        self._path = Path(path)
        self._data: Dict[str, List[str]] = {}  # normalized_query → [plugin/cmd, ...]
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except Exception:
            pass

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存拒绝记录失败: {e}")

    @staticmethod
    def _normalize(query: str) -> str:
        return query.strip().lower()

    def get_excluded(self, query: str) -> List[str]:
        """获取该 query 之前被拒绝过的命令列表，格式 ['插件名|命令名', ...]"""
        return list(self._data.get(self._normalize(query), []))

    def add(self, query: str, plugin_name: str, command_name: str):
        """记录一条拒绝"""
        key = self._normalize(query)
        entry = f"{plugin_name}|{command_name}"
        lst = self._data.setdefault(key, [])
        if entry not in lst:
            lst.append(entry)
            self._save()
            logger.info(f"记录拒绝: {query} → {plugin_name}/{command_name}")
