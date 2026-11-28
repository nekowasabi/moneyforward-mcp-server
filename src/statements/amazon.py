"""Amazon order-history matching."""

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

AMAZON_KEYWORDS = ("AMAZON", "ＡＭＡＺＯＮ", "アマゾン", "AMZN", "Amazon.co.jp")

# Map logical field -> candidate header names (Japanese + English).
DEFAULT_COLUMN_MAP: dict[str, tuple[str, ...]] = {
    "order_date": (
        "注文日",
        "注文日時",
        "order date",
        "order-date",
        "date",
        "shipment date",
    ),
    "order_id": ("注文番号", "注文id", "order id", "order-id", "order number"),
    "total": (
        "合計",
        "請求額",
        "金額",
        "total",
        "grand total",
        "amount",
        "item total",
    ),
    "title": (
        "商品名",
        "商品タイトル",
        "品名",
        "title",
        "product name",
        "item title",
        "product title",
    ),
}


@dataclass(frozen=True)
class AmazonOrder:
    """Single Amazon order line."""

    order_date: date | None
    order_id: str
    total: int | None
    title: str


@dataclass(frozen=True)
class GroupedAmazonOrder:
    """One Amazon order (one or more item rows sharing an order id)."""

    order_date: date | None
    order_id: str
    total: int | None
    titles: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        """Joined item titles (compat with single-row use)."""
        return " ".join(self.titles)


def is_amazon_description(description: str) -> bool:
    """Check whether a statement description looks like Amazon.

    AWS cloud bills (e.g. "AMAZON WEB SERVICES") are NOT retail and return False.
    """
    t = description.casefold()
    if "web service" in t or "webservice" in t:
        return False
    for kw in AMAZON_KEYWORDS:
        if kw.casefold() in t:
            return True
    return False


def _resolve_columns(header: list[str], column_map: dict[str, tuple[str, ...]]) -> dict[str, int]:
    lowered = [h.strip().casefold() for h in header]
    resolved: dict[str, int] = {}
    for logical, candidates in column_map.items():
        for cand in candidates:
            if cand.casefold() in lowered:
                resolved[logical] = lowered.index(cand.casefold())
                break
    return resolved


def _parse_amount(raw: str) -> int | None:
    cleaned = raw.replace(",", "").replace("￥", "").replace("¥", "").replace("円", "")
    cleaned = cleaned.strip().replace(" ", "")
    if not cleaned:
        return None
    try:
        # tolerate decimals
        return int(float(cleaned))
    except ValueError:
        return None


def _parse_date(raw: str) -> date | None:
    s = raw.strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%Y年%m月%d日",
    ):
        try:
            return datetime.strptime(s.split(" ")[0], fmt).date()
        except ValueError:
            continue
    # ISO fallback
    try:
        return date.fromisoformat(s.split(" ")[0].split("T")[0])
    except ValueError:
        return None


def parse_amazon_csv(
    path: Path | str,
    column_map: dict[str, tuple[str, ...]] | None = None,
) -> list[AmazonOrder]:
    """Parse Amazon order history CSV with configurable headers."""
    cmap = column_map or DEFAULT_COLUMN_MAP
    p = Path(str(path)).expanduser()
    with open(p, encoding="utf-8-sig", newline="") as f:
        # Sniff: try utf-8-sig first; fall back handled by caller if needed
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return []
        idx = _resolve_columns(header, cmap)
        orders: list[AmazonOrder] = []
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue

            def _col(name: str) -> str:
                i = idx.get(name)
                if i is None or i >= len(row):
                    return ""
                return row[i].strip()

            od = _parse_date(_col("order_date"))
            oid = _col("order_id")
            total = _parse_amount(_col("total"))
            title = _col("title")
            if not oid and not title:
                continue
            orders.append(AmazonOrder(order_date=od, order_id=oid, total=total, title=title))
    return orders


def load_amazon_orders(
    path: Path | str | None = None,
    column_map: dict[str, tuple[str, ...]] | None = None,
) -> list[AmazonOrder]:
    """Load Amazon orders from explicit path or AMAZON_ORDERS_CSV env."""
    raw = str(path) if path is not None else os.environ.get("AMAZON_ORDERS_CSV", "").strip()
    if not raw:
        return []
    p = Path(raw).expanduser()
    if not p.exists():
        return []
    # Try UTF-8 then Shift_JIS
    try:
        return parse_amazon_csv(p, column_map)
    except (UnicodeDecodeError, UnicodeError):
        with open(p, encoding="shift_jis", newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return []
            rows = list(reader)
        # Re-parse with generic logic
        cmap = column_map or DEFAULT_COLUMN_MAP
        idx = _resolve_columns(header, cmap)
        orders: list[AmazonOrder] = []
        for row in rows:
            if not row or all(not c.strip() for c in row):
                continue

            def _col(name: str, _row: list[str] = row) -> str:
                i = idx.get(name)
                if i is None or i >= len(_row):
                    return ""
                return _row[i].strip()

            od = _parse_date(_col("order_date"))
            oid = _col("order_id")
            total = _parse_amount(_col("total"))
            title = _col("title")
            if not oid and not title:
                continue
            orders.append(AmazonOrder(order_date=od, order_id=oid, total=total, title=title))
        return orders


def group_orders_by_id(orders: list[AmazonOrder]) -> list[GroupedAmazonOrder]:
    """Group item rows by order id into logical orders.

    Order total: when all non-null row totals in a group are equal, that value
    is the order-level total; otherwise the sum of item totals. Rows without
    an order id each form their own group. Titles: all item titles of the group.
    """
    groups: dict[str, list[AmazonOrder]] = {}
    order: list[str] = []
    singles: list[AmazonOrder] = []
    for o in orders:
        if not o.order_id:
            singles.append(o)
            continue
        if o.order_id not in groups:
            groups[o.order_id] = []
            order.append(o.order_id)
        groups[o.order_id].append(o)
    grouped: list[GroupedAmazonOrder] = []
    for oid in order:
        rows = groups[oid]
        totals = [r.total for r in rows if r.total is not None]
        total: int | None
        if not totals:
            total = None
        elif len(set(totals)) == 1:
            total = totals[0]
        else:
            total = sum(totals)
        dates = [r.order_date for r in rows if r.order_date is not None]
        # Earliest date is representative for window matching.
        odt = min(dates) if dates else None
        titles = tuple(r.title for r in rows if r.title)
        grouped.append(GroupedAmazonOrder(order_date=odt, order_id=oid, total=total, titles=titles))
    for o in singles:
        titles = (o.title,) if o.title else ()
        grouped.append(
            GroupedAmazonOrder(
                order_date=o.order_date, order_id=o.order_id, total=o.total, titles=titles
            )
        )
    return grouped


def group_decision_key(titles: tuple[str, ...] | list[str]) -> str:
    """Decision key for a grouped order: sorted normalized titles joined with ' | '."""
    from src.statements.decisions import normalize_description

    parts = sorted({normalize_description(t) for t in titles if normalize_description(t)})
    return f"amazon:{' | '.join(parts)}"


def match_amazon_orders(
    description: str,
    amount: int,
    txn_date: date,
    orders: list[AmazonOrder] | list[GroupedAmazonOrder],
    window_days: int = 3,
) -> list[GroupedAmazonOrder]:
    """Find Amazon orders matching amount + date window.

    Amounts compare by absolute value (MF expenses are negative, Amazon totals
    are positive). Item rows sharing an order id are grouped first: the order
    total is the order-level total when present, else the sum of item prices.
    Only called for Amazon-like descriptions; returns possibly empty list.
    """
    if not is_amazon_description(description):
        return []
    if not orders:
        return []
    if orders and isinstance(orders[0], GroupedAmazonOrder):
        grouped = [o for o in orders if isinstance(o, GroupedAmazonOrder)]
    else:
        grouped = group_orders_by_id([o for o in orders if isinstance(o, AmazonOrder)])
    matched: list[GroupedAmazonOrder] = []
    for o in grouped:
        if o.total is not None and abs(o.total) != abs(amount):
            continue
        if o.total is None:
            # Without amount we cannot confidently match; skip
            continue
        if o.order_date is None:
            matched.append(o)
            continue
        delta = abs((txn_date - o.order_date).days)
        if delta <= window_days:
            matched.append(o)
    return matched


def amazon_match_summary(
    description: str,
    amount: int,
    txn_date: date,
    orders: list[AmazonOrder] | list[GroupedAmazonOrder],
    window_days: int = 3,
) -> dict[str, Any]:
    """Summarize Amazon matching outcome for pipeline/report use."""
    if not is_amazon_description(description):
        return {"is_amazon": False, "matches": []}
    matches = match_amazon_orders(description, amount, txn_date, orders, window_days)
    return {
        "is_amazon": True,
        "matches": matches,
        "unique": len(matches) == 1,
    }
