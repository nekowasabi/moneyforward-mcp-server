"""Persistent human-decision store."""

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any


def default_decisions_path() -> Path:
    """Return decisions path from env or default location."""
    raw = os.environ.get("CARD_DECISIONS_PATH", "~/.config/moneyforward-mcp/decisions.json")
    return Path(raw).expanduser()


def normalize_description(text: str) -> str:
    """Normalize merchant description into a stable lookup key.

    NFKC, casefold, strip digits/dates/whitespace noise.
    """
    t = unicodedata.normalize("NFKC", text).casefold()
    # Remove dates like 2026/10/05, 2026-10-05, 10/05
    t = re.sub(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", " ", t)
    t = re.sub(r"\d{1,2}[/-]\d{1,2}", " ", t)
    # Remove digit sequences (order numbers, amounts)
    t = re.sub(r"[0-9０-９]+", " ", t)
    # Collapse whitespace and strip common noise chars
    t = re.sub(r"[\s\-_.,・()（）「」『』\"'★☆※]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class DecisionStore:
    """JSON-backed store of human classification decisions."""

    def __init__(self, path: Path | str | None = None) -> None:
        """Initialize with explicit path or env/default."""
        if path is None:
            self.path = default_decisions_path()
        elif isinstance(path, str):
            self.path = Path(path).expanduser()
        else:
            self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            self._data = {}
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw: Any = json.load(f)
            if isinstance(raw, dict):
                self._data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
            else:
                self._data = {}
        except (json.JSONDecodeError, OSError):
            self._data = {}

    def _save(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def record(
        self,
        description: str,
        classification: str,
        reason: str,
        business_ratio: float | None = None,
    ) -> str:
        """Record a decision; returns the normalized key."""
        self._ensure_loaded()
        key = normalize_description(description)
        self._data[key] = {
            "description": description,
            "classification": classification,
            "reason": reason,
            "business_ratio": business_ratio,
        }
        self._save()
        return key

    def lookup(self, description: str) -> dict[str, Any] | None:
        """Look up a decision by raw description."""
        self._ensure_loaded()
        return self._data.get(normalize_description(description))

    def list(self) -> dict[str, dict[str, Any]]:
        """Return all decisions keyed by normalized key."""
        self._ensure_loaded()
        return dict(self._data)

    def delete(self, description_or_key: str) -> bool:
        """Delete by raw description or normalized key."""
        self._ensure_loaded()
        key = normalize_description(description_or_key)
        direct = description_or_key in self._data
        if direct:
            del self._data[description_or_key]
            self._save()
            return True
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False
