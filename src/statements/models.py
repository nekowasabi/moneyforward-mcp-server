"""Core data models for card statement classification."""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Literal

ClassificationSource = Literal["decision", "rule", "amazon", "default"]


class Classification(StrEnum):
    """Classification bucket for a statement line."""

    BUSINESS = "BUSINESS"
    PERSONAL = "PERSONAL"
    PRORATED = "PRORATED"
    REVIEW = "REVIEW"


@dataclass(frozen=True)
class Transaction:
    """Single MoneyForward ME statement line."""

    date: date
    description: str
    amount: int
    institution: str = ""
    major_category: str = ""
    minor_category: str = ""
    memo: str = ""
    is_transfer: bool = False
    mf_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain dict."""
        return {
            "date": self.date.isoformat(),
            "description": self.description,
            "amount": self.amount,
            "institution": self.institution,
            "major_category": self.major_category,
            "minor_category": self.minor_category,
            "memo": self.memo,
            "is_transfer": self.is_transfer,
            "mf_id": self.mf_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        """Build from plain dict (accepts ISO date string or date)."""
        raw_date = data.get("date", "")
        parsed: date
        if isinstance(raw_date, date):
            parsed = raw_date
        else:
            text = str(raw_date).strip().split(" ")[0].split("T")[0]
            text = text.replace("/", "-").replace(".", "-")
            parsed = date.fromisoformat(text)
        return cls(
            date=parsed,
            description=str(data.get("description", "")),
            amount=int(data.get("amount", 0)),
            institution=str(data.get("institution", "")),
            major_category=str(data.get("major_category", "")),
            minor_category=str(data.get("minor_category", "")),
            memo=str(data.get("memo", "")),
            is_transfer=bool(data.get("is_transfer", False)),
            mf_id=str(data.get("mf_id", "")),
        )


@dataclass(frozen=True)
class ClassificationResult:
    """Classification outcome for one transaction."""

    classification: Classification
    reason: str
    business_ratio: float | None = None
    source: ClassificationSource = "default"

    def business_amount(self, amount: int) -> int:
        """Compute business-portion amount (rounded to int yen)."""
        if self.classification == Classification.BUSINESS:
            return amount
        if self.classification == Classification.PRORATED and self.business_ratio is not None:
            return int(round(amount * self.business_ratio))
        return 0


@dataclass
class ClassifiedRow:
    """Transaction joined with classification, Amazon and receipt info."""

    transaction: Transaction
    result: ClassificationResult
    amazon_order_id: str | None = None
    receipt_file: str | None = None
    receipt_status: str = "not_required"
    decision_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain dict for CSV/MD/MCP output."""
        ratio = self.result.business_ratio
        return {
            "date": self.transaction.date.isoformat(),
            "description": self.transaction.description,
            "institution": self.transaction.institution,
            "amount": self.transaction.amount,
            "classification": self.result.classification.value,
            "business_ratio": "" if ratio is None else str(ratio),
            "business_amount": self.result.business_amount(self.transaction.amount),
            "reason": self.result.reason,
            "amazon_order_id": self.amazon_order_id or "",
            "receipt_file": self.receipt_file or "",
            "receipt_status": self.receipt_status,
            "decision_key": self.decision_key or "",
        }
