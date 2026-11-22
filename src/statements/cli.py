"""CLI for card statement classification (offline-first)."""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from src.statements.amazon import is_amazon_description, load_amazon_orders, match_amazon_orders
from src.statements.decisions import DecisionStore
from src.statements.models import Classification, Transaction
from src.statements.pipeline import classify_month
from src.statements.receipts import LocalFolderReceiptSource
from src.statements.report import default_report_dir, report_paths, write_csv, write_markdown
from src.statements.rules import Rule, default_rules_path, load_rules


def parse_month(value: str) -> tuple[int, int]:
    """Parse YYYY-MM into (year, month)."""
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid month format (expected YYYY-MM): {value}")
    year = int(parts[0])
    month = int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid month: {value}")
    return year, month


def load_transactions_csv(path: Path | str) -> list[Transaction]:
    """Load MoneyForward CSV export (Shift_JIS or UTF-8) into Transactions."""
    from src.browser.scraper import parse_mf_csv_bytes

    p = Path(str(path)).expanduser()
    raw = p.read_bytes()
    dicts = parse_mf_csv_bytes(raw)
    return [Transaction.from_dict(d) for d in dicts]


def _resolve_rules(rules_path: str | None) -> list[Rule]:
    if rules_path:
        return load_rules(Path(rules_path).expanduser())
    return load_rules(default_rules_path())


def cmd_classify(args: argparse.Namespace) -> int:
    """Run classify subcommand."""
    year, month = parse_month(args.month)
    txns = load_transactions_csv(args.statement_csv)
    rules = _resolve_rules(getattr(args, "rules", None))
    decisions = DecisionStore(
        getattr(args, "decisions", None) or None,
    )
    amazon_orders = []
    if getattr(args, "amazon_csv", None):
        amazon_orders = load_amazon_orders(args.amazon_csv)
    receipt_root = getattr(args, "receipts_root", None)
    source = LocalFolderReceiptSource(receipt_root) if receipt_root else None
    if source is None:
        # Also honour env RECEIPTS_ROOT implicitly
        env_source = LocalFolderReceiptSource()
        if env_source.root is not None:
            source = env_source
        else:
            source = None

    rows = classify_month(txns, rules, decisions, amazon_orders, source)

    fmt = getattr(args, "format", "both")
    base_dir = getattr(args, "output_dir", None) or default_report_dir()
    paths = report_paths(year, month, base_dir)
    if fmt in ("csv", "both"):
        write_csv(rows, paths["csv"])
        print(str(paths["csv"]))
    if fmt in ("md", "both"):
        write_markdown(rows, year, month, paths["md"])
        print(str(paths["md"]))
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """Record a human decision."""
    store = DecisionStore(getattr(args, "decisions", None) or None)
    classification = Classification(str(args.classification))
    ratio = getattr(args, "ratio", None)
    ratio_f: float | None = float(ratio) if ratio is not None else None
    amazon_title = getattr(args, "amazon_item_title", None)
    merchant_wide = bool(getattr(args, "merchant_wide", False))
    try:
        key = store.record(
            description=str(args.description),
            classification=classification.value,
            reason=str(args.reason),
            business_ratio=ratio_f,
            amazon_item_title=amazon_title,
            merchant_wide=merchant_wide,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(key)
    return 0


def cmd_decisions_list(args: argparse.Namespace) -> int:
    """List decisions."""
    store = DecisionStore(getattr(args, "decisions", None) or None)
    import json as _json

    print(_json.dumps(store.list(), ensure_ascii=False, indent=2))
    return 0


def review_rows(
    rows: list[Any],
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    store: DecisionStore | None = None,
    amazon_orders: list[Any] | None = None,
    amazon_window_days: int = 3,
    merchant_wide: bool = False,
) -> int:
    """Interactively review REVIEW rows; testable via injected input_fn."""
    target = store or DecisionStore()
    orders = list(amazon_orders or [])
    saved = 0
    for row in rows:
        result = row.result
        if result.classification != Classification.REVIEW:
            continue
        txn = row.transaction
        is_amazon = is_amazon_description(txn.description)
        unique_titles: tuple[str, ...] | None = None
        if is_amazon:
            candidates = (
                match_amazon_orders(
                    txn.description, txn.amount, txn.date, orders, amazon_window_days
                )
                if orders
                else []
            )
            # Also honour an already-matched order id on the row when orders
            # are not passed (e.g. classify_month ran with orders already).
            if len(candidates) == 1:
                unique_titles = (
                    tuple(candidates[0].titles)
                    if candidates[0].titles
                    else ((candidates[0].title,) if candidates[0].title else ())
                )
            elif getattr(row, "amazon_order_id", None) and getattr(row, "decision_key", None):
                # Row was matched during classify; title unknown here, so we
                # cannot safely record without the title list.
                unique_titles = None
            if unique_titles is None and not merchant_wide:
                print_fn(
                    "Amazon明細は注文の一意な一致がないため、決定を記憶できません。"
                    "この場限りの分類として扱います。"
                )
                print_fn(f"{txn.date} {txn.description} {txn.amount} : {result.reason}")
                print_fn("one-off classification [BUSINESS/PERSONAL/PRORATED/skip]?")
                try:
                    answer = input_fn("> ").strip().upper()
                except EOFError:
                    break
                if answer in ("", "SKIP", "S"):
                    continue
                if answer not in ("BUSINESS", "PERSONAL", "PRORATED", "REVIEW"):
                    print_fn(f"unknown: {answer}, skipped")
                    continue
                ratio: float | None = None
                if answer == "PRORATED":
                    print_fn("business ratio (0-1)?")
                    try:
                        raw = input_fn("> ").strip()
                    except EOFError:
                        break
                    try:
                        ratio = float(raw)
                    except ValueError:
                        print_fn("invalid ratio, skipped")
                        continue
                print_fn("reason?")
                try:
                    reason = input_fn("> ").strip()
                except EOFError:
                    break
                if not reason:
                    reason = "human review (one-off)"
                # One-off: update in memory, do not persist.
                from src.statements.models import ClassificationResult

                row.result = ClassificationResult(
                    classification=Classification(answer),
                    reason=reason,
                    business_ratio=ratio,
                    source="default",
                )
                continue
        print_fn(f"{txn.date} {txn.description} {txn.amount} : {result.reason}")
        print_fn("classification [BUSINESS/PERSONAL/PRORATED/skip]?")
        try:
            answer = input_fn("> ").strip().upper()
        except EOFError:
            break
        if answer in ("", "SKIP", "S"):
            continue
        if answer not in ("BUSINESS", "PERSONAL", "PRORATED", "REVIEW"):
            print_fn(f"unknown: {answer}, skipped")
            continue
        ratio = None
        if answer == "PRORATED":
            print_fn("business ratio (0-1)?")
            try:
                raw = input_fn("> ").strip()
            except EOFError:
                break
            try:
                ratio = float(raw)
            except ValueError:
                print_fn("invalid ratio, skipped")
                continue
        print_fn("reason?")
        try:
            reason = input_fn("> ").strip()
        except EOFError:
            break
        if not reason:
            reason = "human review"
        try:
            if is_amazon and unique_titles is not None:
                target.record(
                    txn.description,
                    answer,
                    reason,
                    ratio,
                    amazon_item_title=list(unique_titles)
                    if len(unique_titles) != 1
                    else unique_titles[0],
                )
            elif is_amazon and merchant_wide:
                target.record(txn.description, answer, reason, ratio, merchant_wide=True)
            elif is_amazon:
                # Should not reach here (handled above), but guard.
                print_fn("決定を記憶できませんでした（一意な注文一致なし）。")
                continue
            else:
                target.record(txn.description, answer, reason, ratio)
        except ValueError as e:
            print_fn(f"決定を記憶できませんでした: {e}")
            continue
        saved += 1
    print_fn(f"saved {saved} decisions")
    return saved


def cmd_review(args: argparse.Namespace) -> int:
    """Run review subcommand."""
    txns = load_transactions_csv(args.statement_csv)
    rules = _resolve_rules(getattr(args, "rules", None))
    store = DecisionStore(getattr(args, "decisions", None) or None)
    amazon_csv = getattr(args, "amazon_csv", None)
    orders = load_amazon_orders(amazon_csv) if amazon_csv else []
    merchant_wide = bool(getattr(args, "merchant_wide", False))
    rows = classify_month(txns, rules, store, orders, None)
    review_rows(rows, input, print, store, orders, 3, merchant_wide)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(prog="card-statements")
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="Classify offline CSV export")
    p_classify.add_argument("--month", required=True, help="YYYY-MM")
    p_classify.add_argument("--statement-csv", required=True)
    p_classify.add_argument("--amazon-csv", default=None)
    p_classify.add_argument("--receipts-root", default=None)
    p_classify.add_argument("--rules", default=None)
    p_classify.add_argument("--decisions", default=None)
    p_classify.add_argument("--output-dir", default=None)
    p_classify.add_argument("--format", default="both", choices=["csv", "md", "both"])
    p_classify.set_defaults(func=cmd_classify)

    p_decide = sub.add_parser("decide", help="Record a decision")
    p_decide.add_argument("--description", required=True)
    p_decide.add_argument(
        "--classification",
        required=True,
        choices=["BUSINESS", "PERSONAL", "PRORATED", "REVIEW"],
    )
    p_decide.add_argument("--ratio", default=None, type=float)
    p_decide.add_argument("--reason", required=True)
    p_decide.add_argument("--decisions", default=None)
    p_decide.add_argument("--amazon-item-title", default=None)
    p_decide.add_argument("--merchant-wide", action="store_true")
    p_decide.set_defaults(func=cmd_decide)

    p_list = sub.add_parser("decisions", help="List decisions")
    p_list.add_argument("--decisions", default=None)
    p_list.set_defaults(func=cmd_decisions_list)

    p_review = sub.add_parser("review", help="Interactively review REVIEW rows")
    p_review.add_argument("--month", required=True)
    p_review.add_argument("--statement-csv", required=True)
    p_review.add_argument("--rules", default=None)
    p_review.add_argument("--decisions", default=None)
    p_review.add_argument("--amazon-csv", default=None)
    p_review.add_argument("--merchant-wide", action="store_true")
    p_review.set_defaults(func=cmd_review)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
