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


def amazon_decision_key(item_title: str) -> str:
    """Return decision key for an Amazon order item title."""
    return f"amazon:{normalize_description(item_title)}"


def _is_amazon_description(text: str) -> bool:
    """Local Amazon check to avoid circular import at module load."""
    try:
        from src.statements.amazon import is_amazon_description
    except ImportError:
        t = unicodedata.normalize("NFKC", text).casefold()
        return any(k.casefold() in t for k in ("amazon", "アマゾン", "amzn"))
    return is_amazon_description(text)


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
        amazon_item_title: str | list[str] | tuple[str, ...] | None = None,
        merchant_wide: bool = False,
    ) -> str:
        """Record a decision; returns the normalized key.

        For Amazon-looking descriptions, an amazon_item_title is required and
        the key is ``amazon:<normalized title>`` — unless merchant_wide=True,
        which explicitly stores a merchant-level decision. Pass a list/tuple
        of titles for a grouped multi-item order; the key is then
        ``amazon:<sorted normalized titles joined with " | ">``.
        """
        self._ensure_loaded()
        if amazon_item_title is not None:
            if isinstance(amazon_item_title, (list, tuple)):
                from src.statements.amazon import group_decision_key

                key = group_decision_key(tuple(amazon_item_title))
                self._data[key] = {
                    "description": " | ".join(list(amazon_item_title)),
                    "classification": classification,
                    "reason": reason,
                    "business_ratio": business_ratio,
                    "amazon_item_titles": list(amazon_item_title),
                }
                self._save()
                return key
            key = amazon_decision_key(amazon_item_title)
            self._data[key] = {
                "description": amazon_item_title,
                "classification": classification,
                "reason": reason,
                "business_ratio": business_ratio,
                "amazon_item_title": amazon_item_title,
            }
            self._save()
            return key
        if _is_amazon_description(description) and not merchant_wide:
            raise ValueError("Amazon descriptions require amazon_item_title or merchant_wide=True")
        key = normalize_description(description)
        entry: dict[str, Any] = {
            "description": description,
            "classification": classification,
            "reason": reason,
            "business_ratio": business_ratio,
        }
        if _is_amazon_description(description) and merchant_wide:
            entry["merchant_wide"] = True
        self._data[key] = entry
        self._save()
        return key

    def lookup(
        self,
        description: str,
        amazon_item_title: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a decision by raw description.

        When amazon_item_title is given, looks up the amazon: key.
        Merchant-level decisions for Amazon descriptions are ignored
        unless they were recorded with merchant_wide=True.
        """
        self._ensure_loaded()
        if amazon_item_title is not None:
            return self._data.get(amazon_decision_key(amazon_item_title))
        if _is_amazon_description(description):
            hit = self._data.get(normalize_description(description))
            if hit is not None and hit.get("merchant_wide"):
                return hit
            return None
        return self._data.get(normalize_description(description))

    def list(self) -> dict[str, dict[str, Any]]:
        """Return all decisions keyed by normalized key."""
        self._ensure_loaded()
        return dict(self._data)

    def delete(
        self,
        description_or_key: str,
        amazon_item_title: str | None = None,
    ) -> bool:
        """Delete by raw description, normalized key, or amazon title."""
        self._ensure_loaded()
        if amazon_item_title is not None:
            key = amazon_decision_key(amazon_item_title)
            if key in self._data:
                del self._data[key]
                self._save()
                return True
            return False
        direct = description_or_key in self._data
        if direct:
            del self._data[description_or_key]
            self._save()
            return True
        # amazon: keys are stored normalized already; try as-is
        if description_or_key.startswith("amazon:"):
            norm_suffix = normalize_description(description_or_key[len("amazon:") :])
            full = f"amazon:{norm_suffix}"
            if full in self._data:
                del self._data[full]
                self._save()
                return True
            return False
        key = normalize_description(description_or_key)
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False
