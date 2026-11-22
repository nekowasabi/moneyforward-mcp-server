"""MCP tools for card statement classification."""

from datetime import date
from pathlib import Path
from typing import Any

import structlog

from src.statements.amazon import load_amazon_orders
from src.statements.decisions import DecisionStore
from src.statements.models import Classification, ClassifiedRow, Transaction
from src.statements.pipeline import classify_month
from src.statements.receipts import LocalFolderReceiptSource
from src.statements.report import report_paths, write_csv, write_markdown
from src.statements.rules import default_rules_path, load_rules
from src.tools.common import build_error_response, cached_tool_call

logger = structlog.get_logger(__name__)


def _transaction_from_dict(data: dict[str, Any]) -> Transaction:
    raw_date = str(data.get("date", ""))
    parsed: date
    try:
        parsed = date.fromisoformat(raw_date.split(" ")[0].split("T")[0])
    except ValueError:
        # Try YYYY/MM/DD or YYYYMMDD
        cleaned = raw_date.replace("/", "-").replace(".", "-")
        try:
            parsed = date.fromisoformat(cleaned)
        except ValueError:
            parsed = date(1970, 1, 1)
    amount = data.get("amount", 0)
    try:
        amount_int = int(amount)
    except (ValueError, TypeError):
        amount_int = 0
    return Transaction(
        date=parsed,
        description=str(data.get("description", "")),
        amount=amount_int,
        institution=str(data.get("institution", data.get("保有金融機関", ""))),
        major_category=str(
            data.get("major_category", data.get("category", data.get("大項目", "")))
        ),
        minor_category=str(data.get("minor_category", data.get("中項目", ""))),
        memo=str(data.get("memo", data.get("メモ", ""))),
        is_transfer=bool(data.get("is_transfer", False)),
        mf_id=str(data.get("mf_id", data.get("ID", ""))),
    )


def _row_to_dict(row: ClassifiedRow) -> dict[str, Any]:
    return row.to_dict()


async def list_financial_institutions(
    scraper: Any,
    cache_manager: Any,
) -> dict[str, Any]:
    """List 保有金融機関 from MoneyForward accounts page."""
    logger.info("list_financial_institutions_called")
    try:

        async def scrape() -> dict[str, Any]:
            institutions = await scraper.list_financial_institutions()
            return {"institutions": institutions, "count": len(institutions)}

        return await cached_tool_call(
            cache_manager=cache_manager,
            cache_key="financial_institutions",
            scrape_fn=scrape,
        )
    except Exception as e:
        logger.error("list_financial_institutions_failed", error=str(e), exc_info=True)
        return build_error_response(
            message=f"Failed to list institutions: {str(e)}",
            error_type="SCRAPING_ERROR",
        )


async def list_monthly_transactions(
    scraper: Any,
    cache_manager: Any,
    year: int,
    month: int,
    institution: str | None = None,
) -> dict[str, Any]:
    """List monthly transactions, optionally filtered by institution."""
    logger.info("list_monthly_transactions_called", year=year, month=month)
    try:
        if not 1 <= month <= 12:
            return build_error_response(
                message=f"Invalid month: {month}", error_type="VALIDATION_ERROR"
            )
        cache_key = f"monthly_transactions_{year:04d}_{month:02d}_{institution or 'all'}"

        async def scrape() -> dict[str, Any]:
            txns = await scraper.get_monthly_transactions(year, month, institution)
            return {
                "year": year,
                "month": month,
                "institution": institution,
                "transactions": txns,
                "count": len(txns),
            }

        return await cached_tool_call(
            cache_manager=cache_manager,
            cache_key=cache_key,
            scrape_fn=scrape,
        )
    except Exception as e:
        logger.error("list_monthly_transactions_failed", error=str(e), exc_info=True)
        return build_error_response(
            message=f"Failed to list monthly transactions: {str(e)}",
            error_type="SCRAPING_ERROR",
        )


async def classify_card_statement(
    scraper: Any,
    cache_manager: Any,
    year: int,
    month: int,
    institution: str | None = None,
    amazon_csv_path: str | None = None,
    write_report: bool = False,
) -> dict[str, Any]:
    """Classify card statement lines for a month."""
    logger.info("classify_card_statement_called", year=year, month=month)
    try:
        if not 1 <= month <= 12:
            return build_error_response(
                message=f"Invalid month: {month}", error_type="VALIDATION_ERROR"
            )
        cache_key = f"card_classification_{year:04d}_{month:02d}_{institution or 'all'}"

        async def scrape() -> dict[str, Any]:
            raw = await scraper.get_monthly_transactions(year, month, institution)
            txns = [_transaction_from_dict(d) for d in raw]
            rules = load_rules(default_rules_path())
            decisions = DecisionStore()
            orders = (
                load_amazon_orders(amazon_csv_path) if amazon_csv_path else load_amazon_orders()
            )
            local_source = LocalFolderReceiptSource()
            source: LocalFolderReceiptSource | None = (
                local_source if local_source.root is not None else None
            )
            rows = classify_month(txns, rules, decisions, orders, source)
            data: dict[str, Any] = {
                "year": year,
                "month": month,
                "institution": institution,
                "rows": [_row_to_dict(r) for r in rows],
                "count": len(rows),
            }
            if write_report:
                paths = report_paths(year, month)
                write_csv(rows, paths["csv"])
                write_markdown(rows, year, month, paths["md"])
                data["report_csv"] = str(paths["csv"])
                data["report_md"] = str(paths["md"])
            return data

        # Classification depends on local rules/decisions; cache briefly
        cached = await cache_manager.get(cache_key)
        if cached and not write_report:
            from src.tools.common import build_success_response

            return build_success_response(cached, source="cache", cached=True)
        data = await scrape()
        try:
            await cache_manager.set(cache_key, data)
        except Exception:
            pass
        from src.tools.common import build_success_response

        return build_success_response(data, source="scraping", cached=False)
    except Exception as e:
        logger.error("classify_card_statement_failed", error=str(e), exc_info=True)
        return build_error_response(
            message=f"Failed to classify statement: {str(e)}",
            error_type="CLASSIFICATION_ERROR",
        )


async def record_classification_decision(
    description: str,
    classification: str,
    reason: str,
    business_ratio: float | None = None,
    amazon_item_title: str | None = None,
    merchant_wide: bool = False,
) -> dict[str, Any]:
    """Record a human classification decision."""
    logger.info("record_classification_decision_called")
    try:
        cls = Classification(classification)
    except ValueError:
        return build_error_response(
            message=f"Invalid classification: {classification}",
            error_type="VALIDATION_ERROR",
        )
    if cls == Classification.PRORATED and business_ratio is None:
        return build_error_response(
            message="business_ratio is required for PRORATED",
            error_type="VALIDATION_ERROR",
        )
    try:
        store = DecisionStore()
        key = store.record(
            description, cls.value, reason, business_ratio, amazon_item_title, merchant_wide
        )
        from src.tools.common import build_success_response

        return build_success_response(
            {"key": key, "classification": cls.value}, source="local", cached=False
        )
    except ValueError as e:
        return build_error_response(
            message=str(e),
            error_type="VALIDATION_ERROR",
        )
    except Exception as e:
        logger.error("record_decision_failed", error=str(e), exc_info=True)
        return build_error_response(
            message=f"Failed to record decision: {str(e)}",
            error_type="STORAGE_ERROR",
        )


async def list_classification_decisions() -> dict[str, Any]:
    """List all human classification decisions."""
    try:
        store = DecisionStore()
        from src.tools.common import build_success_response

        decisions = store.list()
        return build_success_response(
            {"decisions": decisions, "count": len(decisions)},
            source="local",
            cached=False,
        )
    except Exception as e:
        logger.error("list_decisions_failed", error=str(e), exc_info=True)
        return build_error_response(
            message=f"Failed to list decisions: {str(e)}",
            error_type="STORAGE_ERROR",
        )


def _unused_path_hint() -> Path:
    """Keep Path import used for type-checkers in offline contexts."""
    return Path(".")
