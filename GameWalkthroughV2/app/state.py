from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class StateStore:
    """Persists per-game navigation targets shared by the webserver and the viewer.

    The webserver only tracks "which guide page each game is currently on" —
    no chapter/page/pagination state anymore (the frame renders the real page).

    Schema (written to <data>/state.json):
        {
            "active_game": "<last detected game>" or null,
            "games": {
                "<game>": {
                    "url": str,          # 当前攻略页面 URL
                    "image_src": str,    # 定位目标图片（可选）
                    "title": str,        # 章节标题（可选）
                    "updated_at": float
                }
            },
            "last_update": float
        }

    Older state files (chapter/page schema) are read tolerantly: entries that
    still carry only chapter/page are treated as "no target" unless they have
    a url.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"active_game": None, "games": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("games"), dict):
            self._data["games"] = raw["games"]
        if "active_game" in raw:
            self._data["active_game"] = raw["active_game"]
        if isinstance(raw.get("last_update"), (int, float)):
            self._data["last_update"] = raw["last_update"]

    def _save(self) -> None:
        self._data["last_update"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def set_active_game(self, game: str | None) -> None:
        with self._lock:
            self._data["active_game"] = str(game) if game else None
            self._save()

    def get_active_game(self) -> str | None:
        with self._lock:
            value = self._data.get("active_game")
            return str(value) if value else None

    def set_target(
        self,
        game: str,
        url: str,
        *,
        image_src: str = "",
        title: str = "",
        image_index: int | None = None,
    ) -> None:
        with self._lock:
            games = self._data.setdefault("games", {})
            entry = games.setdefault(str(game), {})
            entry["url"] = str(url or "").strip()
            if image_src:
                entry["image_src"] = str(image_src).strip()
            else:
                entry.pop("image_src", None)
            if title:
                entry["title"] = str(title).strip()
            else:
                entry.pop("title", None)
            if image_index is not None and image_index >= 0:
                entry["image_index"] = int(image_index)
            else:
                entry.pop("image_index", None)
            entry["updated_at"] = time.time()
            self._save()

    def get_target(self, game: str) -> dict[str, Any] | None:
        with self._lock:
            games = self._data.get("games") or {}
            entry = games.get(str(game))
            if not isinstance(entry, dict) or not entry.get("url"):
                return None
            return {
                "url": str(entry.get("url") or ""),
                "image_src": str(entry.get("image_src") or ""),
                "title": str(entry.get("title") or ""),
                "image_index": int(entry["image_index"]) if "image_index" in entry else -1,
            }

    def list_games(self) -> list[str]:
        with self._lock:
            games = self._data.get("games") or {}
            return sorted(games.keys())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_game": self._data.get("active_game"),
                "games": self._data.get("games") or {},
                "last_update": self._data.get("last_update"),
            }
