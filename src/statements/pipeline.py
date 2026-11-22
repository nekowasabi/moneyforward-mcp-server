"""Classification pipeline: decision > amazon+rules > rules > REVIEW."""

from typing import Any

import structlog

from src.statements.amazon import (
    AmazonOrder,
    is_amazon_description,
    match_amazon_orders,
)
from src.statements.decisions import DecisionStore
from src.statements.models import (
    Classification,
    ClassificationResult,
    ClassifiedRow,
    Transaction,
)
from src.statements.receipts import ReceiptSource, find_matching_receipt
from src.statements.rules import Rule, apply_rules

logger = structlog.get_logger(__name__)


class NullReceiptSource:
    """Receipt source that always returns empty (no receipts required)."""

    def list_receipts(self, year: int, month: int) -> list[Any]:
        """Return empty list."""
        return []


def _receipt_status_for(
    classification: Classification,
    receipt_file: str | None,
) -> str:
    if classification in (Classification.BUSINESS, Classification.PRORATED):
        return "matched" if receipt_file else "missing"
    return "matched" if receipt_file else "not_required"


def classify_month(
    transactions: list[Transaction],
    rules: list[Rule],
    decisions: DecisionStore | None = None,
    amazon_orders: list[AmazonOrder] | None = None,
    receipt_source: ReceiptSource | None = None,
    amazon_window_days: int = 3,
    receipt_window_days: int = 3,
) -> list[ClassifiedRow]:
    """Classify a month of transactions.

    Order: decision > amazon+rules > rules > REVIEW.
    """
    orders = amazon_orders or []
    rows: list[ClassifiedRow] = []
    for txn in transactions:
        # 1. Human decision first
        if decisions is not None:
            hit = decisions.lookup(txn.description)
            if hit is not None:
                try:
                    cls = Classification(str(hit.get("classification", "REVIEW")))
                except ValueError:
                    cls = Classification.REVIEW
                ratio_raw = hit.get("business_ratio")
                ratio = float(ratio_raw) if ratio_raw is not None else None
                result = ClassificationResult(
                    classification=cls,
                    reason=str(hit.get("reason", "human decision")),
                    business_ratio=ratio,
                    source="decision",
                )
                amazon_id: str | None = None
                receipt_file: str | None = None
                if receipt_source is not None:
                    found = find_matching_receipt(
                        txn.date, txn.amount, receipt_source, receipt_window_days
                    )
                    receipt_file = found.file_name if found else None
                rows.append(
                    ClassifiedRow(
                        transaction=txn,
                        result=result,
                        amazon_order_id=amazon_id,
                        receipt_file=receipt_file,
                        receipt_status=_receipt_status_for(cls, receipt_file),
                    )
                )
                continue

        # 2/3. Amazon-aware rule matching, then plain rules
        amazon_titles: tuple[str, ...] = ()
        matched_order_id: str | None = None
        amazon_matched = False
        if orders and is_amazon_description(txn.description):
            matches = match_amazon_orders(
                txn.description, txn.amount, txn.date, orders, amazon_window_days
            )
            if len(matches) == 1:
                amazon_matched = True
                matched_order_id = matches[0].order_id
                amazon_titles = (matches[0].title,)
                rule_result = apply_rules(txn, rules, amazon_titles)
                if rule_result.classification != Classification.REVIEW:
                    result = ClassificationResult(
                        classification=rule_result.classification,
                        reason=rule_result.reason,
                        business_ratio=rule_result.business_ratio,
                        source="amazon",
                    )
                else:
                    result = ClassificationResult(
                        classification=Classification.REVIEW,
                        reason="amazon matched but no rule decided",
                        business_ratio=None,
                        source="amazon",
                    )
            elif len(matches) > 1:
                result = ClassificationResult(
                    classification=Classification.REVIEW,
                    reason="ambiguous amazon matches",
                    business_ratio=None,
                    source="default",
                )
            else:
                result = ClassificationResult(
                    classification=Classification.REVIEW,
                    reason="amazon description but no order match",
                    business_ratio=None,
                    source="default",
                )
                # Still try plain rules? Spec: amazon+rules then rules.
                # If no order match, fall through to plain rule matching.
                plain = apply_rules(txn, rules, ())
                if plain.classification != Classification.REVIEW:
                    result = ClassificationResult(
                        classification=plain.classification,
                        reason=plain.reason,
                        business_ratio=plain.business_ratio,
                        source="rule",
                    )
        else:
            rule_result = apply_rules(txn, rules, ())
            if rule_result.classification != Classification.REVIEW:
                result = rule_result
            else:
                result = ClassificationResult(
                    classification=Classification.REVIEW,
                    reason=rule_result.reason,
                    business_ratio=None,
                    source="default",
                )
            _ = amazon_matched  # silence unused in this branch

        receipt_name: str | None = None
        if receipt_source is not None:
            found = find_matching_receipt(txn.date, txn.amount, receipt_source, receipt_window_days)
            receipt_name = found.file_name if found else None

        rows.append(
            ClassifiedRow(
                transaction=txn,
                result=result,
                amazon_order_id=matched_order_id,
                receipt_file=receipt_name,
                receipt_status=_receipt_status_for(result.classification, receipt_name),
            )
        )
    return rows
