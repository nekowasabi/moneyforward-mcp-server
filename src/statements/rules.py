"""Rule loading and matching for card statement classification."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from src.statements.models import Classification, ClassificationResult, Transaction


def default_rules_path() -> Path:
    """Return rules path from env or default location."""
    raw = os.environ.get("CARD_RULES_PATH", "~/.config/moneyforward-mcp/card_rules.yaml")
    return Path(raw).expanduser()


@dataclass(frozen=True)
class Rule:
    """Single classification rule (first match wins)."""

    name: str
    classification: Classification
    reason: str
    business_ratio: float | None = None
    description_keywords: tuple[str, ...] = ()
    description_regex: str | None = None
    major_category: tuple[str, ...] = ()
    minor_category: tuple[str, ...] = ()
    institutions: tuple[str, ...] = ()
    amazon_item_keywords: tuple[str, ...] = ()
    _compiled: Any = field(default=None, repr=False, compare=False)

    def matches(
        self,
        txn: Transaction,
        amazon_titles: tuple[str, ...] = (),
    ) -> bool:
        """Check whether this rule matches a transaction."""
        has_constraint = (
            bool(self.description_keywords)
            or bool(self.description_regex)
            or bool(self.major_category)
            or bool(self.minor_category)
            or bool(self.institutions)
            or bool(self.amazon_item_keywords)
        )
        if not has_constraint:
            return False

        if self.institutions:
            inst = txn.institution.casefold()
            if not any(s.casefold() in inst or inst in s.casefold() for s in self.institutions):
                # Also try exact/substring either direction; fall back to strict compare
                if txn.institution not in self.institutions:
                    # Require at least one institution token to match
                    matched = any(tok.casefold() == inst for tok in self.institutions)
                    if not matched and not any(tok.casefold() in inst for tok in self.institutions):
                        return False

        if self.major_category and txn.major_category not in self.major_category:
            return False
        if self.minor_category and txn.minor_category not in self.minor_category:
            return False

        if self.description_keywords:
            desc = txn.description.casefold()
            if not any(k.casefold() in desc for k in self.description_keywords):
                return False

        if self.description_regex:
            try:
                if re.search(self.description_regex, txn.description) is None:
                    return False
            except re.error:
                return False

        if self.amazon_item_keywords:
            if not amazon_titles:
                return False
            joined = " ".join(amazon_titles).casefold()
            if not any(k.casefold() in joined for k in self.amazon_item_keywords):
                return False

        return True


def _parse_rule(data: dict[str, Any]) -> Rule:
    classification = Classification(str(data.get("classification", "REVIEW")))
    ratio_raw = data.get("business_ratio")
    ratio: float | None = None
    if ratio_raw is not None:
        ratio = float(ratio_raw)
    match = data.get("match", {}) if isinstance(data.get("match", {}), dict) else {}
    compiled = None
    regex = match.get("description_regex") or data.get("description_regex")
    if regex:
        try:
            compiled = re.compile(str(regex))
        except re.error:
            compiled = None

    def _tuple(key: str) -> tuple[str, ...]:
        v = match.get(key, data.get(key, []))
        if v is None:
            return ()
        if isinstance(v, str):
            return (v,)
        return tuple(str(x) for x in list(v))

    return Rule(
        name=str(data.get("name", "unnamed")),
        classification=classification,
        reason=str(data.get("reason", "")),
        business_ratio=ratio,
        description_keywords=_tuple("description_keywords"),
        description_regex=str(regex) if regex else None,
        major_category=_tuple("major_category") or _tuple("major_categories"),
        minor_category=_tuple("minor_category") or _tuple("minor_categories"),
        institutions=_tuple("institutions") or _tuple("institution"),
        amazon_item_keywords=_tuple("amazon_item_keywords"),
        _compiled=compiled,
    )


def load_rules(path: Path | str | None = None) -> list[Rule]:
    """Load rules from YAML file. Returns empty list when file is missing."""
    p = Path(path).expanduser() if path else default_rules_path()
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        raw: Any = yaml.safe_load(f)
    if not isinstance(raw, dict):
        return []
    items = raw.get("rules", [])
    if not isinstance(items, list):
        return []
    rules: list[Rule] = []
    for item in items:
        if isinstance(item, dict):
            try:
                rules.append(_parse_rule(item))
            except (ValueError, KeyError):
                continue
    return rules


def apply_rules(
    txn: Transaction,
    rules: list[Rule],
    amazon_titles: tuple[str, ...] = (),
) -> ClassificationResult:
    """Apply rules in order; first match wins, else REVIEW."""
    for rule in rules:
        if rule.matches(txn, amazon_titles):
            return ClassificationResult(
                classification=rule.classification,
                reason=rule.reason or f"rule: {rule.name}",
                business_ratio=rule.business_ratio,
                source="rule",
            )
    return ClassificationResult(
        classification=Classification.REVIEW,
        reason="no matching rule",
        business_ratio=None,
        source="default",
    )
