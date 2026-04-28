from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any


class ChatStateError(ValueError):
    """Raised when chat cursor state cannot satisfy a request."""


class ChatState:
    """Server-side client cursor and alias state for chats."""

    def __init__(self, bos_dir: str | Path | None = None, path: str | Path | None = None) -> None:
        if path is not None:
            self._path = Path(path).expanduser().resolve()
        elif bos_dir is not None:
            self._path = Path(bos_dir).expanduser().resolve() / "state" / "chats.json"
        else:
            self._path = None
        self._data: dict[str, dict[str, str]] = {"client_cursors": {}, "aliases": {}}
        self._loaded = False

    def resolve_for_client(self, client_id: str, supplied_chat_id: str | None = None) -> str:
        client_id = self._require_nonempty("client_id", client_id)
        if supplied_chat_id:
            chat_id = self.resolve_alias_or_id(supplied_chat_id)
            self.set_cursor(client_id, chat_id)
            return chat_id

        cursor = self.get_cursor(client_id)
        if cursor:
            return cursor
        return self.new_chat_for_client(client_id)

    def get_cursor(self, client_id: str) -> str | None:
        client_id = self._require_nonempty("client_id", client_id)
        data = self._read()
        cursor = data["client_cursors"].get(client_id)
        return cursor or None

    def set_cursor(self, client_id: str, chat_id: str) -> None:
        client_id = self._require_nonempty("client_id", client_id)
        chat_id = self._require_nonempty("chat_id", chat_id)
        if is_task_chat_id(chat_id):
            raise ChatStateError("Task-owned chat ids cannot be used as ordinary client cursors.")
        data = self._read()
        data["client_cursors"][client_id] = chat_id
        self._write()

    def new_chat_for_client(self, client_id: str) -> str:
        chat_id = uuid.uuid4().hex
        self.set_cursor(client_id, chat_id)
        return chat_id

    def set_alias(self, alias: str, chat_id: str, *, force: bool = False) -> str:
        normalized = normalize_alias(alias)
        chat_id = self._require_nonempty("chat_id", chat_id)
        if is_task_chat_id(chat_id):
            raise ChatStateError("Task-owned chat ids cannot be used as ordinary chat aliases.")
        data = self._read()
        existing = data["aliases"].get(normalized)
        if existing and existing != chat_id and not force:
            raise ChatStateError(f"Alias {normalized!r} already points to chat {existing!r}.")
        data["aliases"][normalized] = chat_id
        self._write()
        return normalized

    def delete_alias(self, alias: str) -> bool:
        normalized = normalize_alias(alias)
        data = self._read()
        existed = normalized in data["aliases"]
        data["aliases"].pop(normalized, None)
        if existed:
            self._write()
        return existed

    def resolve_alias_or_id(self, value: str) -> str:
        value = self._require_nonempty("chat_id", value)
        data = self._read()
        try:
            alias = normalize_alias(value)
        except ChatStateError:
            alias = ""
        return data["aliases"].get(alias, value)

    def list_aliases(self) -> dict[str, str]:
        return dict(self._read()["aliases"])

    @staticmethod
    def _require_nonempty(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ChatStateError(f"{name} must be non-empty.")
        return value.strip()

    def _read(self) -> dict[str, dict[str, str]]:
        if self._path is None and self._loaded:
            return self._data
        self._loaded = True
        if self._path is None:
            return self._data
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._data
        except Exception as exc:
            raise ChatStateError(f"Could not read chat state: {exc}") from exc
        if not isinstance(raw, dict):
            raise ChatStateError("Chat state must be a JSON object.")
        self._data = {
            "client_cursors": self._string_map(raw.get("client_cursors")),
            "aliases": self._string_map(raw.get("aliases")),
        }
        return self._data

    def _write(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _string_map(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): raw_value
            for key, raw_value in value.items()
            if isinstance(key, str) and key and isinstance(raw_value, str) and raw_value
        }


def normalize_alias(alias: str) -> str:
    if not isinstance(alias, str):
        raise ChatStateError("Alias must be a string.")
    normalized = re.sub(r"\s+", "-", alias.strip().lower())
    if not normalized:
        raise ChatStateError("Alias must be non-empty.")
    if not re.fullmatch(r"[a-z0-9_.-]+", normalized):
        raise ChatStateError("Alias may only contain letters, numbers, dashes, underscores, and dots.")
    return normalized


def is_task_chat_id(chat_id: str) -> bool:
    return isinstance(chat_id, str) and chat_id.startswith("task:")
