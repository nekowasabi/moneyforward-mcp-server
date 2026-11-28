"""Receipt matching against local folder or Google Drive API."""

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReceiptInfo:
    """Parsed receipt file reference."""

    file_name: str
    receipt_date: date | None
    amount: int | None
    vendor: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize."""
        return {
            "file_name": self.file_name,
            "receipt_date": self.receipt_date.isoformat() if self.receipt_date else "",
            "amount": self.amount,
            "vendor": self.vendor,
        }


_RECEIPT_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-/．.]?(?P<m>\d{2})[-/．.]?(?P<d>\d{2})"),
    re.compile(r"(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日"),
)

_AMOUNT_RE = re.compile(r"(?P<n>[0-9][0-9,]*)\s*(?:円|￥|¥)?")


def parse_receipt_filename(file_name: str) -> ReceiptInfo:
    """Parse receipt date/amount/vendor from 電帳法-style file names.

    Examples:
      20261005_取引先_1234.pdf
      2026-10-05_vendor_1,234円.pdf
    Amount is optional; returns None when absent.
    """
    stem = Path(file_name).stem
    receipt_date: date | None = None
    for pat in _RECEIPT_DATE_PATTERNS:
        m = pat.search(stem)
        if m:
            try:
                receipt_date = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
            except ValueError:
                receipt_date = None
            break
    # Remove the date portion to find amount/vendor
    rest = stem
    if receipt_date is not None:
        # Remove first date-like token only
        for pat in _RECEIPT_DATE_PATTERNS:
            rest_new, n = pat.subn(" ", rest, count=1)
            if n:
                rest = rest_new
                break
    amount: int | None = None
    # Find all number tokens; prefer the last one with 円/¥ or largest
    candidates: list[int] = []
    for m in _AMOUNT_RE.finditer(rest):
        raw = m.group("n").replace(",", "")
        try:
            candidates.append(int(raw))
        except ValueError:
            continue
    if candidates:
        # Heuristic: amount is usually the last numeric token
        last = re.findall(r"[0-9][0-9,]*", rest)
        if last:
            try:
                amount = int(last[-1].replace(",", ""))
            except ValueError:
                amount = candidates[-1]
        else:
            amount = candidates[-1]
        # Guard: single tiny token without 円 that looks like an id -> keep it
        # but spec says tolerate missing amount; keep parsed value anyway.
    vendor = re.sub(r"[0-9,円￥¥\s\-_．.]+", " ", rest)
    vendor = re.sub(r"\s+", " ", vendor).strip(" _-")
    return ReceiptInfo(file_name=file_name, receipt_date=receipt_date, amount=amount, vendor=vendor)


class ReceiptSource(Protocol):
    """Source of receipt files for a given month."""

    def list_receipts(self, year: int, month: int) -> list[ReceiptInfo]:
        """List receipts for YYYY-MM (plus adjacent-month fallback handled by caller)."""
        ...


def _month_folder_name(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


class LocalFolderReceiptSource:
    """Receipts from a local folder (e.g. Google Drive for desktop sync)."""

    def __init__(self, root: Path | str | None = None) -> None:
        """Initialize with explicit root or RECEIPTS_ROOT env."""
        raw = str(root) if root is not None else os.environ.get("RECEIPTS_ROOT", "")
        self.root = Path(raw).expanduser() if raw else None

    def _month_dir(self, year: int, month: int) -> Path | None:
        if self.root is None:
            return None
        return self.root / "電子取引" / _month_folder_name(year, month)

    def list_receipts(self, year: int, month: int) -> list[ReceiptInfo]:
        """List receipts in <root>/電子取引/YYYY-MM."""
        d = self._month_dir(year, month)
        if d is None or not d.exists() or not d.is_dir():
            return []
        out: list[ReceiptInfo] = []
        for child in sorted(d.iterdir()):
            if child.is_file():
                out.append(parse_receipt_filename(child.name))
        return out


class GoogleDriveApiReceiptSource:
    """Receipts via Google Drive API v3 using httpx."""

    def __init__(
        self,
        access_token: str | None = None,
        root_folder_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """Initialize. Token from arg or GOOGLE_DRIVE_ACCESS_TOKEN env."""
        token = (
            access_token
            if access_token is not None
            else os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN", "")
        )
        self._access_token = token
        env_root = os.environ.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")
        self._root_folder_id = root_folder_id if root_folder_id is not None else (env_root or None)
        self._client = client
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0)
        return self._client

    def _find_folder(self, client: httpx.Client, name: str, parent_id: str | None) -> str | None:
        query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        if parent_id:
            query += f" and '{parent_id}' in parents"
        resp = client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=self._headers(),
            params={"q": query, "fields": "files(id,name)", "pageSize": 10},
        )
        resp.raise_for_status()
        data: Any = resp.json()
        files = data.get("files", [])
        if not files:
            return None
        first = files[0]
        return str(first.get("id"))

    def _list_files(self, client: httpx.Client, folder_id: str) -> list[ReceiptInfo]:
        resp = client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=self._headers(),
            params={
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "files(id,name)",
                "pageSize": 200,
            },
        )
        resp.raise_for_status()
        data: Any = resp.json()
        out: list[ReceiptInfo] = []
        for item in data.get("files", []):
            name = str(item.get("name", ""))
            if name:
                out.append(parse_receipt_filename(name))
        return out

    def list_receipts(self, year: int, month: int) -> list[ReceiptInfo]:
        """List receipts under 電子取引/YYYY-MM via Drive API."""
        if not self._access_token:
            return []
        client = self._get_client()
        try:
            base = self._find_folder(client, "電子取引", self._root_folder_id)
            if base is None:
                return []
            month_id = self._find_folder(client, _month_folder_name(year, month), base)
            if month_id is None:
                return []
            return self._list_files(client, month_id)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            logger.warning("drive_receipt_list_failed", error=str(e))
            return []

    def close(self) -> None:
        """Close owned client."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None


def _candidate_months(txn_date: date) -> list[tuple[int, int]]:
    months = [(txn_date.year, txn_date.month)]
    # Previous month
    if txn_date.month == 1:
        months.append((txn_date.year - 1, 12))
    else:
        months.append((txn_date.year, txn_date.month - 1))
    # Next month
    if txn_date.month == 12:
        months.append((txn_date.year + 1, 1))
    else:
        months.append((txn_date.year, txn_date.month + 1))
    return months


def find_matching_receipt(
    txn_date: date,
    amount: int,
    source: ReceiptSource,
    window_days: int = 3,
) -> ReceiptInfo | None:
    """Find a receipt matching date (±window) and amount (when present).

    Checks adjacent month folders when the window crosses a month boundary.
    Amount equality is required only when the receipt has an amount.
    """
    seen: dict[str, ReceiptInfo] = {}
    months = _candidate_months(txn_date)
    for y, m in months:
        # Skip adjacent months unless the window actually crosses the boundary
        if (y, m) != (txn_date.year, txn_date.month):
            crosses = False
            if (y, m) == months[1]:  # previous month
                # distance from txn_date to last day of prev month
                first_of_month = date(txn_date.year, txn_date.month, 1)
                delta = (txn_date - first_of_month).days
                crosses = delta < window_days
            else:  # next month
                import calendar as _cal

                last_day = _cal.monthrange(txn_date.year, txn_date.month)[1]
                last = date(txn_date.year, txn_date.month, last_day)
                delta = (last - txn_date).days
                crosses = delta < window_days
            if not crosses:
                continue
        try:
            receipts = source.list_receipts(y, m)
        except Exception:
            continue
        for r in receipts:
            if r.file_name in seen:
                continue
            seen[r.file_name] = r
    best: ReceiptInfo | None = None
    for r in seen.values():
        if r.receipt_date is None:
            continue
        if abs((txn_date - r.receipt_date).days) > window_days:
            continue
        if r.amount is not None and abs(r.amount) != abs(amount):
            continue
        # Prefer amount-bearing matches, then closest date
        if best is None:
            best = r
            continue
        best_has_amount = best.amount is not None
        cur_has_amount = r.amount is not None
        if cur_has_amount and not best_has_amount:
            best = r
        elif cur_has_amount == best_has_amount:
            assert best.receipt_date is not None and r.receipt_date is not None
            if abs((txn_date - r.receipt_date).days) < abs((txn_date - best.receipt_date).days):
                best = r
    return best
