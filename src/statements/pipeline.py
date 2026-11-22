"""Classification pipeline: decision > amazon+rules > rules > REVIEW."""

from typing import Any

import structlog

from src.statements.amazon import (
    AmazonOrder,
    GroupedAmazonOrder,
    group_decision_key,
    is_amazon_description,
    match_amazon_orders,
)
from src.statements.decisions import DecisionStore, normalize_description
from src.statements.models import (
    Classification,
    ClassificationResult,
    ClassifiedRow,
    Transaction,
)
from src.statements.receipts import ReceiptSource, find_matching_receipt
from src.statements.rules import Rule, apply_rules

logger = structlog.get_logger(__name__)

EXCLUDED_REASON = "対象外（振替・計算対象外）"
INCOME_REASON = "入金・返金"


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


def _find_receipt_file(
    txn: Transaction,
    receipt_source: ReceiptSource | None,
    receipt_window_days: int,
) -> str | None:
    if receipt_source is None:
        return None
    found = find_matching_receipt(txn.date, txn.amount, receipt_source, receipt_window_days)
    return found.file_name if found else None


def _decision_result(hit: dict[str, Any]) -> ClassificationResult:
    try:
        cls = Classification(str(hit.get("classification", "REVIEW")))
    except ValueError:
        cls = Classification.REVIEW
    ratio_raw = hit.get("business_ratio")
    ratio = float(ratio_raw) if ratio_raw is not None else None
    return ClassificationResult(
        classification=cls,
        reason=str(hit.get("reason", "human decision")),
        business_ratio=ratio,
        source="decision",
    )


def _lookup_grouped_decision(
    decisions: DecisionStore | None, titles: tuple[str, ...] | list[str]
) -> dict[str, Any] | None:
    if decisions is None:
        return None
    key = group_decision_key(tuple(titles))
    data = decisions.list()
    hit = data.get(key)
    if hit is not None:
        return hit
    # Backward compat: single-title groups were stored via amazon:<norm title>.
    # group_decision_key for a single title is identical, so this only helps
    # when titles contain one entry with odd spacing.
    return None


def _income_review() -> ClassificationResult:
    return ClassificationResult(
        classification=Classification.REVIEW,
        reason=INCOME_REASON,
        business_ratio=None,
        source="default",
    )


def classify_month(
    transactions: list[Transaction],
    rules: list[Rule],
    decisions: DecisionStore | None = None,
    amazon_orders: list[AmazonOrder] | list[GroupedAmazonOrder] | None = None,
    receipt_source: ReceiptSource | None = None,
    amazon_window_days: int = 3,
    receipt_window_days: int = 3,
) -> list[ClassifiedRow]:
    """Classify a month of transactions.

    Order: excluded > decision > amazon+rules > rules > REVIEW.

    Excluded (transfer / 計算対象外) rows are not classified and carry
    extra["excluded"]=True with reason 対象外. Positive amounts (income/refunds)
    fall back to REVIEW 入金・返金 when no decision/rule decides.

    For Amazon-looking descriptions the order match runs first. Item rows
    sharing an order id are grouped: matching uses the order total (order-level
    total when present, else the sum of item prices) and all item titles feed
    rule matching; the decision key is the sorted, normalized titles joined
    with " | ". Without a unique match, Amazon lines stay REVIEW unless a
    plain rule or merchant-wide decision applies.
    """
    orders = amazon_orders or []
    rows: list[ClassifiedRow] = []
    for txn in transactions:
        # --- Excluded: transfers and non-calc-target rows ---
        if txn.is_excluded:
            rows.append(
                ClassifiedRow(
                    transaction=txn,
                    result=ClassificationResult(
                        classification=Classification.REVIEW,
                        reason=EXCLUDED_REASON,
                        business_ratio=None,
                        source="default",
                    ),
                    amazon_order_id=None,
                    receipt_file=None,
                    receipt_status="not_required",
                    decision_key=None,
                    extra={"excluded": True},
                )
            )
            continue

        is_amazon = is_amazon_description(txn.description)
        matches: list[GroupedAmazonOrder] = []
        if is_amazon and orders:
            matches = match_amazon_orders(
                txn.description, txn.amount, txn.date, orders, amazon_window_days
            )

        # --- Amazon branch: unique match may hit an item-level decision ---
        if is_amazon:
            if len(matches) == 1:
                group = matches[0]
                titles = tuple(group.titles) if group.titles else (group.title,)
                dkey = group_decision_key(titles)
                if decisions is not None:
                    hit = _lookup_grouped_decision(decisions, titles)
                    if hit is not None:
                        result = _decision_result(hit)
                        receipt_file = _find_receipt_file(txn, receipt_source, receipt_window_days)
                        rows.append(
                            ClassifiedRow(
                                transaction=txn,
                                result=result,
                                amazon_order_id=group.order_id,
                                receipt_file=receipt_file,
                                receipt_status=_receipt_status_for(
                                    result.classification, receipt_file
                                ),
                                decision_key=dkey,
                            )
                        )
                        continue
                    # Explicit merchant-wide fallback (only stored with flag).
                    mhit = decisions.lookup(txn.description)
                    if mhit is not None:
                        result = _decision_result(mhit)
                        receipt_file = _find_receipt_file(txn, receipt_source, receipt_window_days)
                        rows.append(
                            ClassifiedRow(
                                transaction=txn,
                                result=result,
                                amazon_order_id=group.order_id,
                                receipt_file=receipt_file,
                                receipt_status=_receipt_status_for(
                                    result.classification, receipt_file
                                ),
                                decision_key=normalize_description(txn.description),
                            )
                        )
                        continue
                # amazon + rules (all item titles feed rule matching)
                amazon_titles = titles
                rule_result = apply_rules(txn, rules, amazon_titles)
                if rule_result.classification != Classification.REVIEW:
                    result = ClassificationResult(
                        classification=rule_result.classification,
                        reason=rule_result.reason,
                        business_ratio=rule_result.business_ratio,
                        source="amazon",
                    )
                elif txn.amount > 0:
                    result = _income_review()
                else:
                    result = ClassificationResult(
                        classification=Classification.REVIEW,
                        reason="amazon matched but no rule decided",
                        business_ratio=None,
                        source="amazon",
                    )
                receipt_file = _find_receipt_file(txn, receipt_source, receipt_window_days)
                rows.append(
                    ClassifiedRow(
                        transaction=txn,
                        result=result,
                        amazon_order_id=group.order_id,
                        receipt_file=receipt_file,
                        receipt_status=_receipt_status_for(result.classification, receipt_file),
                        decision_key=dkey,
                    )
                )
                continue
            if len(matches) > 1:
                # Explicit merchant-wide decision may still apply.
                if decisions is not None:
                    mhit = decisions.lookup(txn.description)
                    if mhit is not None:
                        mresult = _decision_result(mhit)
                        mreceipt = _find_receipt_file(txn, receipt_source, receipt_window_days)
                        rows.append(
                            ClassifiedRow(
                                transaction=txn,
                                result=mresult,
                                amazon_order_id=None,
                                receipt_file=mreceipt,
                                receipt_status=_receipt_status_for(
                                    mresult.classification, mreceipt
                                ),
                                decision_key=normalize_description(txn.description),
                            )
                        )
                        continue
                ids = ", ".join(o.order_id for o in matches if o.order_id)
                result = ClassificationResult(
                    classification=Classification.REVIEW,
                    reason=f"ambiguous amazon matches: {ids}"
                    if ids
                    else "ambiguous amazon matches",
                    business_ratio=None,
                    source="default",
                )
                receipt_file = _find_receipt_file(txn, receipt_source, receipt_window_days)
                rows.append(
                    ClassifiedRow(
                        transaction=txn,
                        result=result,
                        amazon_order_id=None,
                        receipt_file=receipt_file,
                        receipt_status=_receipt_status_for(result.classification, receipt_file),
                        decision_key=None,
                    )
                )
                continue
            # No unique match: merchant-wide decision may apply; else plain rules.
            if decisions is not None:
                mhit = decisions.lookup(txn.description)
                if mhit is not None:
                    mresult = _decision_result(mhit)
                    mreceipt = _find_receipt_file(txn, receipt_source, receipt_window_days)
                    rows.append(
                        ClassifiedRow(
                            transaction=txn,
                            result=mresult,
                            amazon_order_id=None,
                            receipt_file=mreceipt,
                            receipt_status=_receipt_status_for(mresult.classification, mreceipt),
                            decision_key=normalize_description(txn.description),
                        )
                    )
                    continue
            plain = apply_rules(txn, rules, ())
            if plain.classification != Classification.REVIEW:
                result = plain
            elif txn.amount > 0:
                result = _income_review()
            else:
                # Keep the amazon-specific reason when no rule decides.
                if orders:
                    result = ClassificationResult(
                        classification=Classification.REVIEW,
                        reason="amazon description but no order match",
                        business_ratio=None,
                        source="default",
                    )
                else:
                    result = ClassificationResult(
                        classification=plain.classification,
                        reason=plain.reason,
                        business_ratio=None,
                        source="default",
                    )
            receipt_file = _find_receipt_file(txn, receipt_source, receipt_window_days)
            rows.append(
                ClassifiedRow(
                    transaction=txn,
                    result=result,
                    amazon_order_id=None,
                    receipt_file=receipt_file,
                    receipt_status=_receipt_status_for(result.classification, receipt_file),
                    decision_key=None,
                )
            )
            continue

        # --- Non-Amazon branch: merchant decision first ---
        dkey = normalize_description(txn.description)
        if decisions is not None:
            hit = decisions.lookup(txn.description)
            if hit is not None:
                result = _decision_result(hit)
                receipt_file = _find_receipt_file(txn, receipt_source, receipt_window_days)
                rows.append(
                    ClassifiedRow(
                        transaction=txn,
                        result=result,
                        amazon_order_id=None,
                        receipt_file=receipt_file,
                        receipt_status=_receipt_status_for(result.classification, receipt_file),
                        decision_key=dkey,
                    )
                )
                continue

        rule_result = apply_rules(txn, rules, ())
        if rule_result.classification != Classification.REVIEW:
            result = rule_result
        elif txn.amount > 0:
            result = _income_review()
        else:
            result = ClassificationResult(
                classification=Classification.REVIEW,
                reason=rule_result.reason,
                business_ratio=None,
                source="default",
            )
        receipt_file = _find_receipt_file(txn, receipt_source, receipt_window_days)
        rows.append(
            ClassifiedRow(
                transaction=txn,
                result=result,
                amazon_order_id=None,
                receipt_file=receipt_file,
                receipt_status=_receipt_status_for(result.classification, receipt_file),
                decision_key=dkey,
            )
        )
    return rows
