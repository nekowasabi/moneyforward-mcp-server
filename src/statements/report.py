"""Monthly reporting: CSV and Markdown with summary."""

import csv
import os
from pathlib import Path
from typing import Any

from src.statements.models import Classification, ClassifiedRow

CSV_HEADERS = [
    "date",
    "description",
    "institution",
    "amount",
    "classification",
    "business_ratio",
    "business_amount",
    "reason",
    "amazon_order_id",
    "receipt_file",
    "receipt_status",
    "decision_key",
]


def default_report_dir() -> Path:
    """Return report dir from env or default location."""
    raw = os.environ.get("CARD_REPORT_DIR", "~/.local/share/moneyforward-mcp/reports")
    return Path(raw).expanduser()


def is_excluded_row(r: ClassifiedRow) -> bool:
    """Check whether a row was excluded (transfer / non-calc-target)."""
    try:
        return bool(r.extra.get("excluded"))
    except AttributeError:
        return False


def summarize(rows: list[ClassifiedRow]) -> dict[str, Any]:
    """Build summary totals per classification.

    Amounts are shown as positive spend (abs); the raw signed amount stays in
    the row itself. Excluded rows (対象外) are counted separately and do not
    contribute to classification totals.
    """
    totals: dict[str, int] = {c.value: 0 for c in Classification}
    business_total = 0
    review_count = 0
    missing_receipts = 0
    excluded_count = 0
    for r in rows:
        if is_excluded_row(r):
            excluded_count += 1
            continue
        totals[r.result.classification.value] += abs(r.transaction.amount)
        business_total += r.result.business_amount(r.transaction.amount)
        if r.result.classification == Classification.REVIEW:
            review_count += 1
        if (
            r.result.classification
            in (
                Classification.BUSINESS,
                Classification.PRORATED,
            )
            and r.receipt_status == "missing"
        ):
            missing_receipts += 1
    return {
        "count": len([r for r in rows if not is_excluded_row(r)]),
        "totals_by_classification": totals,
        "business_amount_total": business_total,
        "review_count": review_count,
        "missing_receipts": missing_receipts,
        "excluded_count": excluded_count,
        "total_rows": len(rows),
    }


def write_csv(rows: list[ClassifiedRow], path: Path | str) -> Path:
    """Write rows to CSV; returns output path."""
    out = Path(str(path))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.to_dict())
    return out


def render_markdown(rows: list[ClassifiedRow], year: int, month: int) -> str:
    """Render markdown report text."""
    summary = summarize(rows)
    lines: list[str] = []
    lines.append(f"# カード明細振り分け {year:04d}-{month:02d}")
    lines.append("")
    lines.append("## サマリー")
    lines.append("")
    lines.append(f"- 件数: {summary['count']}")
    totals = summary["totals_by_classification"]
    assert isinstance(totals, dict)
    for key in ("BUSINESS", "PERSONAL", "PRORATED", "REVIEW"):
        lines.append(f"- {key}: {totals.get(key, 0)}円")
    lines.append(f"- 事業按分合計: {summary['business_amount_total']}円")
    lines.append(f"- 要確認 (REVIEW): {summary['review_count']}件")
    lines.append(f"- 領収書不足: {summary['missing_receipts']}件")
    lines.append(f"- 対象外: {summary.get('excluded_count', 0)}件")
    lines.append("")
    lines.append("## 明細")
    lines.append("")
    lines.append(
        "| 日付 | 内容 | 金融機関 | 金額 | 分類 | 比率 | 事業額 | 理由 | Amazon注文 | 領収書 | 状態 | 判定キー |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        if is_excluded_row(r):
            continue
        d = r.to_dict()
        lines.append(
            "| {date} | {description} | {institution} | {amount} | "
            "{classification} | {business_ratio} | {business_amount} | "
            "{reason} | {amazon_order_id} | {receipt_file} | {receipt_status} | {decision_key} |".format(
                **{k: str(d.get(k, "")) for k in CSV_HEADERS}
            )
        )
    excluded = [r for r in rows if is_excluded_row(r)]
    if excluded:
        lines.append("")
        lines.append("## 対象外")
        lines.append("")
        lines.append(
            "| 日付 | 内容 | 金融機関 | 金額 | 分類 | 比率 | 事業額 | 理由 | Amazon注文 | 領収書 | 状態 | 判定キー |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in excluded:
            d = r.to_dict()
            lines.append(
                "| {date} | {description} | {institution} | {amount} | "
                "{classification} | {business_ratio} | {business_amount} | "
                "{reason} | {amazon_order_id} | {receipt_file} | {receipt_status} | {decision_key} |".format(
                    **{k: str(d.get(k, "")) for k in CSV_HEADERS}
                )
            )
    lines.append("")
    return "\n".join(lines)


def write_markdown(rows: list[ClassifiedRow], year: int, month: int, path: Path | str) -> Path:
    """Write markdown report; returns output path."""
    out = Path(str(path))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_markdown(rows, year, month))
    return out


def report_paths(year: int, month: int, base_dir: Path | str | None = None) -> dict[str, Path]:
    """Return default csv/md paths for a month."""
    base = Path(str(base_dir)).expanduser() if base_dir else default_report_dir()
    stem = base / f"{year:04d}-{month:02d}"
    return {"csv": stem.with_suffix(".csv"), "md": stem.with_suffix(".md")}
