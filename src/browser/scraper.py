"""Web scraping functionality for MoneyForward ME.

This module provides the MoneyForwardScraper class that handles all data extraction
from MoneyForward ME pages, including total assets, transactions, budget status,
and account refresh operations.
"""

import csv
import io
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import structlog
from playwright.async_api import Page

from src.browser.auth import AuthManager
from src.browser.context import BrowserManager

logger = structlog.get_logger(__name__)


MF_CSV_COLUMNS = [
    "計算対象",
    "日付",
    "内容",
    "金額（円）",
    "保有金融機関",
    "大項目",
    "中項目",
    "メモ",
    "振替",
    "ID",
]


def build_monthly_csv_url(selectors: dict[str, Any], year: int, month: int) -> str:
    """Build monthly CSV URL from selectors.yaml template."""
    template = ""
    try:
        template = str(selectors.get("monthly_csv", {}).get("url_template", ""))
    except (AttributeError, TypeError):
        template = ""
    if not template:
        template = "https://moneyforward.com/cf/csv?from={from_date}&month={month}&year={year}"
    from_date = f"{year:04d}/{month:02d}/01"
    return template.format(from_date=from_date, month=month, year=year)


def build_monthly_url(selectors: dict[str, Any], year: int, month: int) -> str:
    """Build monthly HTML URL from selectors.yaml template."""
    template = ""
    try:
        template = str(selectors.get("monthly_transactions", {}).get("url_template", ""))
    except (AttributeError, TypeError):
        template = ""
    if not template:
        try:
            template = str(selectors.get("transactions", {}).get("url", ""))
        except (AttributeError, TypeError):
            template = ""
    if not template or "{from_date}" not in template:
        base = template or "https://moneyforward.com/cf"
        from_date = f"{year:04d}/{month:02d}/01"
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}from={from_date}&month={month}&year={year}"
    from_date = f"{year:04d}/{month:02d}/01"
    return template.format(from_date=from_date, month=month, year=year)


def _normalize_mf_row(row: dict[str, str]) -> dict[str, Any]:
    date_raw = (row.get("日付") or "").strip()
    amount_raw = (row.get("金額（円）") or "0").strip()
    # Amount may include commas; keep sign
    cleaned = re.sub(r"[,\s円￥¥]", "", amount_raw)
    try:
        amount = int(cleaned) if cleaned not in ("", "-", "+") else 0
    except ValueError:
        try:
            amount = int(float(cleaned))
        except ValueError:
            amount = 0
    calc = (row.get("計算対象") or "").strip()
    transfer_raw = (row.get("振替") or "").strip()
    is_transfer = transfer_raw not in ("", "0", "対象外", "-")
    # MF uses "振替" column with values like "" or "振替"; treat truthy text as transfer
    if transfer_raw in ("振替", "○", "1", "true"):
        is_transfer = True
    elif transfer_raw == "":
        is_transfer = False
    return {
        "date": date_raw,
        "description": (row.get("内容") or "").strip(),
        "amount": amount,
        "institution": (row.get("保有金融機関") or "").strip(),
        "major_category": (row.get("大項目") or "").strip(),
        "minor_category": (row.get("中項目") or "").strip(),
        "memo": (row.get("メモ") or "").strip(),
        "is_transfer": is_transfer,
        "mf_id": (row.get("ID") or "").strip(),
        "calc_target": calc,
    }


def parse_mf_csv_text(text: str) -> list[dict[str, Any]]:
    """Parse MF CSV text into transaction dicts (pure, testable)."""
    # Strip BOM if present
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, Any]] = []
    for row in reader:
        if row is None:
            continue
        # Normalize keys (strip spaces)
        norm = {(k or "").strip(): (v or "") for k, v in row.items()}
        if not any(v.strip() for v in norm.values()):
            continue
        out.append(_normalize_mf_row(norm))
    return out


def parse_mf_csv_bytes(data: bytes) -> list[dict[str, Any]]:
    """Parse MF CSV bytes, trying UTF-8 (BOM) then Shift_JIS/CP932."""
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis"):
        try:
            text = data.decode(encoding)
            return parse_mf_csv_text(text)
        except (UnicodeDecodeError, UnicodeError) as e:
            last_error = e
            continue
    raise ValueError(f"Unable to decode MF CSV bytes: {last_error}")


def filter_transactions_by_institution(
    transactions: list[dict[str, Any]], institution: str | None
) -> list[dict[str, Any]]:
    """Filter transactions by institution name (substring, case-insensitive)."""
    if not institution:
        return transactions
    needle = institution.casefold().strip()
    return [t for t in transactions if needle in str(t.get("institution", "")).casefold()]


class _TableParser(HTMLParser):
    """Very defensive HTML table parser for MF transaction fallback."""

    def __init__(self) -> None:
        """Initialize parser state."""
        super().__init__()
        self.in_td = False
        self.in_th = False
        self.current_cell = ""
        self.current_row: list[str] = []
        self.rows: list[list[str]] = []
        self.in_tr = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track table cell starts."""
        if tag == "tr":
            self.in_tr = True
            self.current_row = []
        elif tag in ("td", "th"):
            self.in_td = tag == "td"
            self.in_th = tag == "th"
            self.current_cell = ""

    def handle_endtag(self, tag: str) -> None:
        """Track table cell ends."""
        if tag in ("td", "th"):
            self.current_row.append(self.current_cell.strip())
            self.in_td = False
            self.in_th = False
        elif tag == "tr":
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_tr = False
            self.current_row = []

    def handle_data(self, data: str) -> None:
        """Collect cell text."""
        if self.in_td or self.in_th:
            self.current_cell += data


def parse_mf_transactions_html(html: str) -> list[dict[str, Any]]:
    """Parse MF transaction HTML table into dicts (fallback path)."""
    parser = _TableParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for cells in parser.rows:
        if len(cells) < 3:
            continue
        # Heuristic: skip header rows containing 日付/内容/金額
        joined = "".join(cells)
        if "日付" in joined and "内容" in joined:
            continue
        # Map positionally: date, description, amount, category...
        date_raw = cells[0] if len(cells) > 0 else ""
        desc = cells[1] if len(cells) > 1 else ""
        amount_raw = cells[2] if len(cells) > 2 else "0"
        category = cells[3] if len(cells) > 3 else ""
        if not desc and not date_raw:
            continue
        cleaned = re.sub(r"[,\s円￥¥]", "", amount_raw)
        try:
            amount = int(cleaned) if cleaned not in ("", "-", "+") else 0
        except ValueError:
            amount = 0
        out.append(
            {
                "date": date_raw.strip(),
                "description": desc.strip(),
                "amount": amount,
                "institution": "",
                "major_category": category.strip(),
                "minor_category": "",
                "memo": "",
                "is_transfer": False,
                "mf_id": "",
                "calc_target": "",
            }
        )
    return out


def parse_institutions_html(html: str) -> list[dict[str, Any]]:
    """Parse institution entries from accounts HTML (defensive)."""
    parser = _TableParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for cells in parser.rows:
        if not cells:
            continue
        name = cells[0].strip() if len(cells) > 0 else ""
        if not name or name in ("金融機関", "口座", "保有金融機関"):
            continue
        entry: dict[str, Any] = {"name": name}
        if len(cells) > 1:
            entry["type"] = cells[1].strip()
        if len(cells) > 2:
            entry["status"] = cells[2].strip()
        out.append(entry)
    return out


class ScraperError(Exception):
    """Raised when scraping operations fail."""

    pass


class MoneyForwardScraper:
    """Scraper for MoneyForward ME data extraction.

    This class provides methods to extract various financial data from
    MoneyForward ME pages including assets, transactions, budget, and
    account refresh status.

    Attributes:
        browser: BrowserManager instance for page access.
        auth: AuthManager instance for authentication.
        selectors: CSS selectors loaded from selectors.yaml.
    """

    def __init__(
        self,
        browser: BrowserManager,
        auth: AuthManager,
        selectors: dict[str, Any],
    ) -> None:
        """Initialize MoneyForwardScraper.

        Args:
            browser: BrowserManager instance.
            auth: AuthManager instance.
            selectors: Dictionary of CSS selectors from selectors.yaml.
        """
        self.browser = browser
        self.auth = auth
        self.selectors = selectors

    async def get_total_assets(self) -> dict[str, Any]:
        """Scrape total assets and daily change from portfolio page.

        Returns:
            Dictionary containing:
                - total_assets_jpy: Total assets in JPY (int)
                - daily_change_jpy: Daily change in JPY (int, can be negative)
                - fetched_at: ISO 8601 timestamp string

        Raises:
            ScraperError: If scraping fails.
        """
        logger.info("scraping_total_assets")

        # Ensure authenticated before scraping
        await self.auth.ensure_authenticated()

        page = await self.browser.new_page()
        try:
            # Navigate to portfolio page
            url = self.selectors["portfolio"]["url"]
            await self._navigate_and_wait(page, url)

            # Extract total assets
            total_assets_selector = self.selectors["portfolio"]["total_assets"]
            total_assets_text = await self._extract_text(page, total_assets_selector)

            if not total_assets_text:
                raise ScraperError("Failed to extract total assets")

            total_assets_jpy = self._parse_currency(total_assets_text)

            # Extract daily change
            daily_change_selector = self.selectors["portfolio"]["daily_change"]
            daily_change_text = await self._extract_text(page, daily_change_selector)

            daily_change_jpy = 0
            if daily_change_text:
                daily_change_jpy = self._parse_currency(daily_change_text)

            result = {
                "total_assets_jpy": total_assets_jpy,
                "daily_change_jpy": daily_change_jpy,
                "fetched_at": self._get_current_timestamp(),
            }

            logger.info(
                "total_assets_scraped_successfully",
                total_assets_jpy=total_assets_jpy,
                daily_change_jpy=daily_change_jpy,
            )

            return result

        except Exception as e:
            logger.error(
                "total_assets_scraping_failed",
                error=str(e),
                exc_info=True,
            )
            raise ScraperError(f"Failed to scrape total assets: {e}") from e

        finally:
            await page.close()

    async def get_recent_transactions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Scrape recent transactions from cash flow page.

        Args:
            limit: Maximum number of transactions to retrieve (default: 20).

        Returns:
            List of transaction dictionaries, each containing:
                - date: Transaction date (str)
                - description: Transaction description (str)
                - amount: Transaction amount in JPY (int)
                - category: Transaction category / 大項目 (str)
                - subcategory: Transaction subcategory / 中項目 (str)

        Raises:
            ScraperError: If scraping fails.
        """
        logger.info("scraping_recent_transactions", limit=limit)

        # Ensure authenticated before scraping
        await self.auth.ensure_authenticated()

        page = await self.browser.new_page()
        try:
            # Navigate to transactions page
            url = self.selectors["transactions"]["url"]
            await self._navigate_and_wait(page, url)

            # Extract transactions table
            table_selector = self.selectors["transactions"]["table"]
            row_selector = self.selectors["transactions"]["rows"]

            # Wait for table to load
            await page.wait_for_selector(table_selector, timeout=10000)

            # Get all transaction rows
            rows = await page.query_selector_all(row_selector)

            transactions = []
            for i, row in enumerate(rows[:limit]):
                try:
                    # Extract transaction data
                    date_text = await row.query_selector(self.selectors["transactions"]["date"])
                    description_text = await row.query_selector(
                        self.selectors["transactions"]["description"]
                    )
                    amount_text = await row.query_selector(self.selectors["transactions"]["amount"])
                    category_text = await row.query_selector(
                        self.selectors["transactions"]["category"]
                    )
                    subcategory_text = await row.query_selector(
                        self.selectors["transactions"]["subcategory"]
                    )

                    transaction = {
                        "date": await date_text.inner_text() if date_text else "",
                        "description": (
                            await description_text.inner_text() if description_text else ""
                        ),
                        "amount": self._parse_currency(
                            await amount_text.inner_text() if amount_text else "0"
                        ),
                        "category": (
                            (await category_text.inner_text()).strip()
                            if category_text
                            else ""
                        ),
                        "subcategory": (
                            (await subcategory_text.inner_text()).strip()
                            if subcategory_text
                            else ""
                        ),
                    }

                    transactions.append(transaction)

                except Exception as e:
                    logger.warning(
                        "transaction_row_parse_error",
                        row_index=i,
                        error=str(e),
                    )
                    continue

            logger.info(
                "transactions_scraped_successfully",
                count=len(transactions),
            )

            return transactions

        except Exception as e:
            logger.error(
                "transactions_scraping_failed",
                error=str(e),
                exc_info=True,
            )
            raise ScraperError(f"Failed to scrape transactions: {e}") from e

        finally:
            await page.close()

    async def list_financial_institutions(self) -> list[dict[str, Any]]:
        """List 保有金融機関 from the accounts page.

        Returns:
            List of dicts with name, type, last_updated/status when available.

        Raises:
            ScraperError: If scraping fails.
        """
        logger.info("scraping_financial_institutions")
        await self.auth.ensure_authenticated()
        page = await self.browser.new_page()
        try:
            accounts_cfg: dict[str, Any] = {}
            if isinstance(self.selectors.get("accounts"), dict):
                accounts_cfg = self.selectors["accounts"]
            url = str(accounts_cfg.get("url", "https://moneyforward.com/accounts"))
            await self._navigate_and_wait(page, url)

            institutions: list[dict[str, Any]] = []
            try:
                item_selector = str(accounts_cfg.get("institution_item", ".account-list-item"))
                name_selector = str(accounts_cfg.get("institution_name", ".account-name"))
                type_selector = str(accounts_cfg.get("institution_type", ".account-type"))
                status_selector = str(accounts_cfg.get("status_indicator", ".account-status"))
                items = await page.query_selector_all(item_selector)
                for item in items:
                    try:
                        name_el = await item.query_selector(name_selector)
                        name = (await name_el.inner_text()).strip() if name_el else ""
                        if not name:
                            continue
                        entry: dict[str, Any] = {"name": name}
                        type_el = await item.query_selector(type_selector)
                        if type_el:
                            entry["type"] = (await type_el.inner_text()).strip()
                        status_el = await item.query_selector(status_selector)
                        if status_el:
                            entry["status"] = (await status_el.inner_text()).strip()
                        institutions.append(entry)
                    except Exception as e:
                        logger.warning("institution_row_parse_error", error=str(e))
                        continue
            except Exception as e:
                logger.warning("institution_selector_parse_failed", error=str(e))

            if not institutions:
                # Fallback: parse raw HTML defensively
                try:
                    html = await page.content()
                    institutions = parse_institutions_html(html)
                except Exception as e:
                    logger.warning("institution_html_fallback_failed", error=str(e))

            logger.info("institutions_scraped_successfully", count=len(institutions))
            return institutions
        except Exception as e:
            logger.error("institutions_scraping_failed", error=str(e), exc_info=True)
            raise ScraperError(f"Failed to list institutions: {e}") from e
        finally:
            await page.close()

    async def get_monthly_transactions(
        self,
        year: int,
        month: int,
        institution: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get transactions for a month, optionally filtered by institution.

        Prefers the CSV endpoint with HTML table fallback.

        Args:
            year: Year (e.g. 2026).
            month: Month 1-12.
            institution: Optional institution (card) name filter.

        Raises:
            ScraperError: If scraping fails.
            ValueError: If year/month are invalid.
        """
        if not 1 <= month <= 12:
            raise ValueError(f"Invalid month: {month}")
        logger.info(
            "scraping_monthly_transactions",
            year=year,
            month=month,
            institution=institution,
        )
        await self.auth.ensure_authenticated()
        csv_url = build_monthly_csv_url(self.selectors, year, month)
        # Try CSV via authenticated request context first
        try:
            context = await self.browser.get_context()
            response = await context.request.get(csv_url)
            if response.ok:
                body = await response.body()
                parsed = parse_mf_csv_bytes(bytes(body))
                return filter_transactions_by_institution(parsed, institution)
            else:
                logger.warning("monthly_csv_request_failed", status=response.status)
        except Exception as e:
            logger.warning("monthly_csv_download_failed", error=str(e))

        # HTML fallback
        page = await self.browser.new_page()
        try:
            html_url = build_monthly_url(self.selectors, year, month)
            await self._navigate_and_wait(page, html_url)
            try:
                html = await page.content()
                parsed = parse_mf_transactions_html(html)
                # HTML fallback lacks institution; filter only if present
                return filter_transactions_by_institution(parsed, institution)
            except Exception as e:
                raise ScraperError(f"Failed HTML fallback for monthly transactions: {e}") from e
        finally:
            await page.close()

    async def trigger_account_refresh(self) -> dict[str, Any]:
        """Trigger account refresh and wait for completion.

        Returns:
            Dictionary containing:
                - status: Refresh status (str)
                - refreshed_at: ISO 8601 timestamp string

        Raises:
            ScraperError: If refresh operation fails.
        """
        logger.info("triggering_account_refresh")

        # Ensure authenticated before operation
        await self.auth.ensure_authenticated()

        page = await self.browser.new_page()
        try:
            # Navigate to accounts page
            url = self.selectors["refresh"]["url"]
            await self._navigate_and_wait(page, url)

            # Click refresh button
            refresh_button = self.selectors["refresh"]["refresh_button"]
            await page.wait_for_selector(refresh_button, timeout=10000)
            await page.click(refresh_button)

            logger.debug("refresh_button_clicked")

            # Wait for refresh to start (brief delay)
            await page.wait_for_timeout(2000)

            # Check status indicator
            status_selector = self.selectors["refresh"]["status_indicator"]
            status_text = await self._extract_text(page, status_selector)

            result = {
                "status": status_text or "refresh_triggered",
                "refreshed_at": self._get_current_timestamp(),
            }

            logger.info("account_refresh_triggered", status=result["status"])

            return result

        except Exception as e:
            logger.error(
                "account_refresh_failed",
                error=str(e),
                exc_info=True,
            )
            raise ScraperError(f"Failed to trigger account refresh: {e}") from e

        finally:
            await page.close()

    async def get_budget_status(self) -> dict[str, Any]:
        """Scrape budget status from spending page.

        Returns:
            Dictionary containing:
                - month: Current month (str)
                - budget: Total budget in JPY (int)
                - spent: Total spent in JPY (int)
                - remaining: Remaining budget in JPY (int)
                - categories: List of category breakdowns

        Raises:
            ScraperError: If scraping fails.
        """
        logger.info("scraping_budget_status")

        # Ensure authenticated before scraping
        await self.auth.ensure_authenticated()

        page = await self.browser.new_page()
        try:
            # Navigate to budget page
            url = self.selectors["budget"]["url"]
            await self._navigate_and_wait(page, url)

            # Extract total budget
            total_budget_text = await self._extract_text(
                page, self.selectors["budget"]["total_budget"]
            )
            total_budget = self._parse_currency(total_budget_text or "0")

            # Extract total spent
            total_spent_text = await self._extract_text(
                page, self.selectors["budget"]["total_spent"]
            )
            total_spent = self._parse_currency(total_spent_text or "0")

            # Calculate remaining
            remaining = total_budget - total_spent

            # Extract category breakdowns
            categories = []
            category_selector = self.selectors["budget"]["categories"]
            category_elements = await page.query_selector_all(category_selector)

            for elem in category_elements:
                try:
                    category_name = await elem.inner_text()
                    categories.append({"name": category_name})
                except Exception as e:
                    logger.warning("category_parse_error", error=str(e))
                    continue

            result = {
                "month": datetime.now().strftime("%Y-%m"),
                "budget": total_budget,
                "spent": total_spent,
                "remaining": remaining,
                "categories": categories,
            }

            logger.info(
                "budget_status_scraped_successfully",
                budget=total_budget,
                spent=total_spent,
                remaining=remaining,
            )

            return result

        except Exception as e:
            logger.error(
                "budget_scraping_failed",
                error=str(e),
                exc_info=True,
            )
            raise ScraperError(f"Failed to scrape budget status: {e}") from e

        finally:
            await page.close()

    async def check_health(self) -> dict[str, Any]:
        """Check health of browser and session.

        Returns:
            Dictionary containing:
                - browser_status: Browser health status (str)
                - session_valid: Whether session is valid (bool)
                - checked_at: ISO 8601 timestamp string

        Raises:
            ScraperError: If health check fails.
        """
        logger.info("checking_health")

        try:
            # Check if browser context is available
            context = await self.browser.get_context()
            browser_status = "ok" if context else "unavailable"

            # Check session validity
            session_valid = await self.auth.is_session_valid()

            result = {
                "browser_status": browser_status,
                "session_valid": session_valid,
                "checked_at": self._get_current_timestamp(),
            }

            logger.info(
                "health_check_completed",
                browser_status=browser_status,
                session_valid=session_valid,
            )

            return result

        except Exception as e:
            logger.error(
                "health_check_failed",
                error=str(e),
                exc_info=True,
            )
            raise ScraperError(f"Health check failed: {e}") from e

    async def update_manual_account_balance(
        self, mf_display_name: str, amount_jpy: int, *, currency: str = "MYR"
    ) -> None:
        """Update a manual account balance on MoneyForward ME.

        Flow:
        1. Navigate to /accounts to find the manual account link by display name
        2. Extract the account's hash ID URL (e.g., /accounts/show_manual/{hash})
        3. Navigate to the account detail page
        4. Use the rollover (残高修正) form to set the new balance

        Args:
            mf_display_name: Display name of the account on MoneyForward ME.
            amount_jpy: New balance in JPY.
            currency: Foreign currency code (e.g., "MYR", "USD").

        Raises:
            ScraperError: If the update operation fails.
        """
        logger.info(
            "updating_manual_account_balance",
            mf_display_name=mf_display_name,
            amount_jpy=amount_jpy,
        )

        await self.auth.ensure_authenticated()

        page = await self.browser.new_page()
        try:
            # Step 1: Go to accounts list to find the manual account URL
            await self._navigate_and_wait(page, "https://moneyforward.com/accounts")

            # Step 2: Find the link matching the display name
            account_link = page.locator(
                f'a[href*="/accounts/show_manual/"]:has-text("{mf_display_name}")'
            )
            count = await account_link.count()

            if count == 0:
                await page.screenshot(path="/tmp/manual_accounts_not_found.png", full_page=True)
                raise ScraperError(
                    f"Account '{mf_display_name}' not found on accounts page. "
                    "Check /tmp/manual_accounts_not_found.png for page state."
                )

            # Get the account URL and navigate
            account_url = await account_link.first.get_attribute("href")
            logger.debug("account_url_found", url=account_url)

            await account_link.first.click()
            await page.wait_for_load_state("networkidle")

            await page.screenshot(path="/tmp/manual_account_detail.png", full_page=True)

            # Step 3: Check if an existing asset entry exists for this account
            # Each entry has a "change" button (btn-asset-action linking to a modal)
            change_btn = page.locator('a.btn-asset-action:not([data-method="delete"])')
            has_existing = await change_btn.count() > 0

            if has_existing:
                # Update existing entry via its edit modal
                logger.debug("updating_existing_entry", mf_display_name=mf_display_name)
                await change_btn.first.click()
                await page.wait_for_timeout(500)

                # The edit modal has the same form fields as the new modal
                value_input = page.locator(
                    '.modal.in input[name="user_asset_det[value]"],'
                    ' .modal.show input[name="user_asset_det[value]"]'
                )
                await value_input.wait_for(state="visible", timeout=5000)
                await value_input.clear()
                await value_input.fill(str(amount_jpy))

                submit_btn = page.locator(
                    '.modal.in input[type="submit"], .modal.show input[type="submit"]'
                )
                await submit_btn.first.click()
                await page.wait_for_load_state("networkidle")
            else:
                # Add new asset entry via modal
                logger.debug("creating_new_entry", mf_display_name=mf_display_name)
                await page.evaluate('$("#modal_asset_new").modal("show")')
                await page.wait_for_timeout(500)

                # Select asset subclass: "外貨預金" (id=3)
                asset_type = self.selectors["manual_accounts"].get("default_asset_subclass_id", "3")
                subclass_select = page.locator(
                    '#modal_asset_new select[name="user_asset_det[asset_subclass_id]"]'
                )
                await subclass_select.wait_for(state="visible", timeout=5000)
                await subclass_select.select_option(value=asset_type)

                # Fill name
                name_input = page.locator('#modal_asset_new input[name="user_asset_det[name]"]')
                await name_input.fill(f"{mf_display_name} ({currency})")

                # Fill amount
                value_input = page.locator('#modal_asset_new input[name="user_asset_det[value]"]')
                await value_input.fill(str(amount_jpy))

                # Submit
                submit_btn = page.locator('#modal_asset_new input[type="submit"]')
                await submit_btn.first.click()
                await page.wait_for_load_state("networkidle")

            await page.screenshot(path="/tmp/manual_account_saved.png", full_page=True)

            logger.info(
                "manual_account_balance_updated",
                mf_display_name=mf_display_name,
                amount_jpy=amount_jpy,
            )

        except ScraperError:
            raise
        except Exception as e:
            logger.error(
                "manual_account_update_failed",
                mf_display_name=mf_display_name,
                error=str(e),
                exc_info=True,
            )
            raise ScraperError(f"Failed to update manual account '{mf_display_name}': {e}") from e

        finally:
            await page.close()

    async def _navigate_and_wait(self, page: Page, url: str) -> None:
        """Navigate to URL and wait for network idle.

        Args:
            page: The page to navigate.
            url: The URL to navigate to.

        Raises:
            Exception: If navigation fails.
        """
        logger.debug("navigating_to_url", url=url)
        await page.goto(url, wait_until="networkidle")
        logger.debug("navigation_complete", url=url)

    async def _extract_text(self, page: Page, selector: str) -> str | None:
        """Extract text content from an element.

        Args:
            page: The page to extract from.
            selector: CSS selector for the element.

        Returns:
            Text content or None if element not found.
        """
        try:
            element = await page.query_selector(selector)
            if element:
                text = await element.inner_text()
                return text.strip()
            return None
        except Exception as e:
            logger.warning(
                "text_extraction_failed",
                selector=selector,
                error=str(e),
            )
            return None

    def _parse_currency(self, text: str) -> int:
        """Parse currency string to integer.

        Converts strings like "¥1,234,567" or "-¥12,345" to integers.

        Args:
            text: Currency string to parse.

        Returns:
            Integer amount in JPY.
        """
        # Remove currency symbols, commas, whitespace, and Japanese text
        cleaned = re.sub(r"[¥円,\s]", "", text)
        # Extract number portion (handles "資産総額：4703541" etc.)
        match = re.search(r"-?\d+", cleaned)
        if match:
            cleaned = match.group(0)
        else:
            cleaned = ""

        # Handle empty or invalid strings
        if not cleaned or cleaned == "-":
            return 0

        try:
            return int(cleaned)
        except ValueError:
            logger.warning("currency_parse_failed", text=text)
            return 0

    def _get_current_timestamp(self) -> str:
        """Get current timestamp in ISO 8601 format with JST timezone.

        Returns:
            ISO 8601 timestamp string.
        """
        jst = timezone(timedelta(hours=9))
        return datetime.now(jst).isoformat()
