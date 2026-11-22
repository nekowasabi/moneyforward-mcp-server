"""Tests for card statement classification (fictional data only)."""

import csv
import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from src.browser.scraper import (
    build_monthly_csv_url,
    build_monthly_url,
    filter_transactions_by_institution,
    parse_institutions_html,
    parse_mf_csv_bytes,
    parse_mf_csv_text,
    parse_mf_transactions_html,
)
from src.statements.amazon import (
    is_amazon_description,
    load_amazon_orders,
    match_amazon_orders,
    parse_amazon_csv,
)
from src.statements.decisions import (
    DecisionStore,
    amazon_decision_key,
    normalize_description,
)
from src.statements.models import (
    Classification,
    ClassificationResult,
    ClassifiedRow,
    Transaction,
)
from src.statements.pipeline import classify_month
from src.statements.receipts import (
    GoogleDriveApiReceiptSource,
    LocalFolderReceiptSource,
    find_matching_receipt,
    parse_receipt_filename,
)
from src.statements.report import (
    report_paths,
    summarize,
    write_csv,
    write_markdown,
)
from src.statements.rules import Rule, apply_rules, load_rules


def _txn(desc: str = "Example Store", amount: int = 1000, **kw) -> Transaction:
    base = {
        "date": date(2026, 10, 5),
        "description": desc,
        "amount": amount,
    }
    base.update(kw)
    return Transaction(
        date=base["date"],
        description=base["description"],
        amount=base["amount"],
        institution=str(kw.get("institution", "Example Card")),
        major_category=str(kw.get("major_category", "")),
        minor_category=str(kw.get("minor_category", "")),
    )


# --- models ---


def test_business_amount_calc():
    assert ClassificationResult(Classification.BUSINESS, "r").business_amount(1000) == 1000
    assert ClassificationResult(Classification.PRORATED, "r", 0.3).business_amount(1000) == 300
    assert ClassificationResult(Classification.PERSONAL, "r").business_amount(1000) == 0
    assert ClassificationResult(Classification.REVIEW, "r").business_amount(500) == 0


def test_classified_row_to_dict():
    row = ClassifiedRow(
        transaction=_txn("Shop", 2000),
        result=ClassificationResult(Classification.PRORATED, "split", 0.2),
    )
    d = row.to_dict()
    assert d["business_amount"] == 400
    assert d["classification"] == "PRORATED"


# --- rules ---


def test_rules_first_match_and_ratio(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(
        "rules:\n"
        "  - name: first\n"
        "    classification: PRORATED\n"
        "    reason: split it\n"
        "    business_ratio: 0.3\n"
        "    match:\n"
        '      description_keywords: [" telecom "]\n'
        "  - name: second\n"
        "    classification: BUSINESS\n"
        "    reason: biz\n"
        "    match:\n"
        '      description_keywords: ["telecom"]\n',
        encoding="utf-8",
    )
    rules = load_rules(p)
    assert len(rules) == 2
    txn = _txn(" TELECOM bill ")
    res = apply_rules(txn, rules)
    assert res.classification == Classification.PRORATED
    assert res.business_ratio == 0.3
    assert res.source == "rule"


def test_rules_unmatched_review():
    res = apply_rules(_txn("mystery shop"), [])
    assert res.classification == Classification.REVIEW
    assert res.reason == "no matching rule"


def test_rule_category_and_regex():
    r = Rule(
        name="t",
        classification=Classification.PERSONAL,
        reason="r",
        major_category=("通信費",),
        description_regex="私用|personal",
    )
    txn = _txn("something personal", major_category="通信費")
    assert r.matches(txn)
    txn2 = _txn("something personal", major_category="食費")
    assert not r.matches(txn2)


def test_example_rules_cover_task_keywords(tmp_path):
    # Mirror the shipped example expectations using fictional amounts
    example = Path("card_rules.yaml.example")
    assert example.exists()
    rules = load_rules(example)
    assert rules

    def classify(desc: str, major: str = "") -> Classification:
        t = _txn(desc, major_category=major)
        return apply_rules(t, rules).classification

    assert classify("携帯料金 10月分", "通信費") == Classification.PRORATED
    assert classify("東京電力 電気代", "水道・光熱費") == Classification.PRORATED
    assert classify("スターバックス 渋谷店") == Classification.PERSONAL
    assert classify("ドトール 新宿店") == Classification.PERSONAL
    assert classify("ATOK Passport") == Classification.REVIEW
    assert classify("Remember The Milk Pro") == Classification.REVIEW
    assert classify("AWS Bill") == Classification.BUSINESS
    assert classify("GitHub Copilot") == Classification.BUSINESS


# --- decisions ---


def test_normalize_description():
    assert normalize_description("  AMAZON 2026/10/05 1234 ") == "amazon"
    assert normalize_description("スターバックス 2026-10-05 渋谷店123") == normalize_description(
        "スターバックス 渋谷店"
    )


def test_decision_persistence_and_precedence(tmp_path, monkeypatch):
    dec_path = tmp_path / "decisions.json"
    monkeypatch.setenv("CARD_DECISIONS_PATH", str(dec_path))
    store = DecisionStore()
    key = store.record("Example Cafe 123", "PERSONAL", "human says cafe", None)
    assert key
    assert store.lookup("Example Cafe 999") is not None
    # persistence
    store2 = DecisionStore()
    assert store2.lookup("example cafe") is not None
    # precedence over rules in pipeline
    rules = [
        Rule(
            name="biz",
            classification=Classification.BUSINESS,
            reason="rule says biz",
            description_keywords=("cafe",),
        )
    ]
    rows = classify_month([_txn("Example Cafe")], rules, store2, [], None)
    assert rows[0].result.classification == Classification.PERSONAL
    assert rows[0].result.source == "decision"
    # file perms
    assert dec_path.exists()
    assert (dec_path.stat().st_mode & 0o777) == 0o600
    assert (dec_path.parent.stat().st_mode & 0o777) == 0o700


def test_decision_list_delete(tmp_path):
    p = tmp_path / "d.json"
    s = DecisionStore(p)
    s.record("Shop A", "BUSINESS", "r", None)
    assert len(s.list()) == 1
    assert s.delete("Shop A")
    assert s.list() == {}


# --- amazon ---


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_amazon_parse_jp_and_en(tmp_path):
    jp = tmp_path / "jp.csv"
    _write_csv(
        jp,
        ["注文日", "注文番号", "合計", "商品名"],
        [["2026-10-04", "ORDER-1", "1500", "文具セット"]],
    )
    orders = parse_amazon_csv(jp)
    assert len(orders) == 1
    assert orders[0].order_id == "ORDER-1"
    assert orders[0].total == 1500

    en = tmp_path / "en.csv"
    _write_csv(
        en,
        ["order date", "order id", "grand total", "product title"],
        [["10/04/2026", "ORDER-2", "2,500", "Printer Paper"]],
    )
    orders2 = parse_amazon_csv(en)
    assert orders2[0].total == 2500


def test_amazon_match_unique_ambiguous_none():
    from src.statements.amazon import AmazonOrder

    orders = [
        AmazonOrder(date(2026, 10, 4), "A-1", 1500, "文具セット"),
        AmazonOrder(date(2026, 10, 5), "A-2", 1500, "プリンタ用紙"),
    ]
    assert is_amazon_description("AMAZON.CO.JP")
    assert is_amazon_description("アマゾン マーケット")
    assert not is_amazon_description("Example Store")
    # ambiguous: two same-amount orders in window
    m = match_amazon_orders("AMAZON", 1500, date(2026, 10, 5), orders, 3)
    assert len(m) == 2
    # unique
    m2 = match_amazon_orders("AMAZON", 1500, date(2026, 10, 4), orders[:1], 3)
    assert len(m2) == 1
    # none: outside window
    m3 = match_amazon_orders("AMAZON", 1500, date(2026, 9, 1), orders, 3)
    assert m3 == []
    # non-amazon description never matches
    assert match_amazon_orders("Shop", 1500, date(2026, 10, 5), orders) == []


def test_load_amazon_orders_env(tmp_path, monkeypatch):
    f = tmp_path / "orders.csv"
    _write_csv(f, ["order date", "order id", "total", "title"], [["2026-10-04", "X", "100", "t"]])
    monkeypatch.setenv("AMAZON_ORDERS_CSV", str(f))
    assert len(load_amazon_orders()) == 1
    monkeypatch.delenv("AMAZON_ORDERS_CSV")
    assert load_amazon_orders() == []


# --- receipts ---


def test_parse_receipt_filename():
    r = parse_receipt_filename("20261005_ExampleVendor_1234.pdf")
    assert r.receipt_date == date(2026, 10, 5)
    assert r.amount == 1234
    assert "ExampleVendor" in r.vendor

    r2 = parse_receipt_filename("2026-10-05_vendor_1,234円.pdf")
    assert r2.receipt_date == date(2026, 10, 5)
    assert r2.amount == 1234

    r3 = parse_receipt_filename("20261005_ExampleVendor.pdf")
    assert r3.amount is None
    assert r3.receipt_date == date(2026, 10, 5)


def test_receipt_match_window_and_boundary(tmp_path):
    from src.statements.receipts import ReceiptInfo

    class FakeSource:
        def __init__(self, by_month: dict):
            self.by_month = by_month

        def list_receipts(self, year: int, month: int) -> list:
            return self.by_month.get((year, month), [])

    src = FakeSource(
        {
            (2026, 10): [ReceiptInfo("20261005_Shop_1000.pdf", date(2026, 10, 5), 1000)],
            (2026, 9): [ReceiptInfo("20260930_Shop_500.pdf", date(2026, 9, 30), 500)],
        }
    )
    found = find_matching_receipt(date(2026, 10, 6), 1000, src, 3)
    assert found is not None
    # outside window
    assert find_matching_receipt(date(2026, 10, 20), 1000, src, 3) is None
    # month boundary: txn 2026-10-01 matches 2026-09-30 receipt
    found2 = find_matching_receipt(date(2026, 10, 1), 500, src, 3)
    assert found2 is not None and found2.amount == 500
    # amount mismatch
    assert find_matching_receipt(date(2026, 10, 6), 9999, src, 3) is None


def test_local_folder_source(tmp_path, monkeypatch):
    root = tmp_path / "drive"
    (root / "電子取引" / "2026-10").mkdir(parents=True)
    (root / "電子取引" / "2026-10" / "20261005_Shop_1000.pdf").write_text("x")
    monkeypatch.setenv("RECEIPTS_ROOT", str(root))
    src = LocalFolderReceiptSource()
    receipts = src.list_receipts(2026, 10)
    assert len(receipts) == 1
    assert receipts[0].amount == 1000


def test_drive_source_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        if "mimeType" in q:
            if "電子取引" in q:
                return httpx.Response(200, json={"files": [{"id": "base", "name": "電子取引"}]})
            return httpx.Response(200, json={"files": [{"id": "m10", "name": "2026-10"}]})
        return httpx.Response(200, json={"files": [{"id": "r1", "name": "20261005_Shop_1000.pdf"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    src = GoogleDriveApiReceiptSource(access_token="token", root_folder_id=None, client=client)
    receipts = src.list_receipts(2026, 10)
    assert len(receipts) == 1
    assert receipts[0].receipt_date == date(2026, 10, 5)


# --- report ---


def test_report_csv_md(tmp_path):
    rows = [
        ClassifiedRow(
            transaction=_txn("Shop A", 1000),
            result=ClassificationResult(Classification.BUSINESS, "biz"),
            receipt_file="20261005_Shop_1000.pdf",
            receipt_status="matched",
        ),
        ClassifiedRow(
            transaction=_txn("Mystery", 500),
            result=ClassificationResult(Classification.REVIEW, "no rule"),
            receipt_status="not_required",
        ),
    ]
    csv_path = tmp_path / "2026-10.csv"
    md_path = tmp_path / "2026-10.md"
    write_csv(rows, csv_path)
    assert csv_path.exists()
    content = csv_path.read_text(encoding="utf-8-sig")
    assert "Shop A" in content
    write_markdown(rows, 2026, 10, md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "REVIEW" in md
    s = summarize(rows)
    assert s["review_count"] == 1
    assert s["missing_receipts"] == 0  # BUSINESS has receipt
    # missing receipt case
    rows2 = [
        ClassifiedRow(
            transaction=_txn("Biz No Receipt", 2000),
            result=ClassificationResult(Classification.BUSINESS, "biz"),
            receipt_status="missing",
        )
    ]
    assert summarize(rows2)["missing_receipts"] == 1
    paths = report_paths(2026, 10, tmp_path)
    assert paths["csv"].name == "2026-10.csv"


# --- scraper pure parsing ---


MF_SAMPLE = (
    "計算対象,日付,内容,金額（円）,保有金融機関,大項目,中項目,メモ,振替,ID\n"
    "対象,2026/10/05,Example Store,-1500,Example Card,食費,外食,,,\n"
    "対象,2026/10/06,Cafe Fictional,-800,Example Card,カフェ,コーヒー,私用,,\n"
)


def test_mf_csv_utf8_and_shiftjis():
    rows = parse_mf_csv_text(MF_SAMPLE)
    assert len(rows) == 2
    assert rows[0]["description"] == "Example Store"
    assert rows[0]["amount"] == -1500
    data_utf8 = MF_SAMPLE.encode("utf-8")
    assert len(parse_mf_csv_bytes(data_utf8)) == 2
    data_sjis = MF_SAMPLE.encode("cp932")
    assert len(parse_mf_csv_bytes(data_sjis)) == 2
    # filter
    assert len(filter_transactions_by_institution(rows, "example card")) == 2
    assert len(filter_transactions_by_institution(rows, "other")) == 0


def test_mf_html_fallback_and_institutions():
    html = (
        "<table><tr><th>日付</th><th>内容</th><th>金額</th></tr>"
        "<tr><td>2026/10/05</td><td>Example Store</td><td>-1500</td></tr></table>"
    )
    rows = parse_mf_transactions_html(html)
    assert len(rows) == 1
    assert rows[0]["description"] == "Example Store"
    inst_html = "<table><tr><td>Example Card</td><td>card</td></tr></table>"
    inst = parse_institutions_html(inst_html)
    assert inst[0]["name"] == "Example Card"
    selectors = {
        "monthly_csv": {"url_template": "https://example.test/csv?from={from_date}"},
        "transactions": {"url": "https://example.test/cf"},
    }
    assert "2026/10/01" in build_monthly_csv_url(selectors, 2026, 10)
    assert "2026" in build_monthly_url(selectors, 2026, 10)


# --- pipeline order ---


def test_pipeline_decision_amazon_rules_review(tmp_path):
    from src.statements.amazon import AmazonOrder

    dec_path = tmp_path / "dec.json"
    store = DecisionStore(dec_path)
    store.record("Decided Shop", "PERSONAL", "human", None)
    rules = [
        Rule(
            name="amazon-paper",
            classification=Classification.BUSINESS,
            reason="paper biz",
            description_keywords=("AMAZON",),
            amazon_item_keywords=("プリンタ用紙",),
        ),
        Rule(
            name="plain",
            classification=Classification.PERSONAL,
            reason="cafe personal",
            description_keywords=("カフェ",),
        ),
    ]
    orders = [AmazonOrder(date(2026, 10, 4), "A-1", 1500, "プリンタ用紙 A4")]
    txns = [
        _txn("Decided Shop", 1000),  # decision wins even though no rule
        Transaction(
            date=date(2026, 10, 5),
            description="AMAZON",
            amount=1500,
            institution="Example Card",
        ),
        _txn("カフェ fictional", 800),
        _txn("Unknown Mystery", 300),
    ]
    rows = classify_month(txns, rules, store, orders, None)
    assert rows[0].result.source == "decision"
    assert rows[1].result.source == "amazon"
    assert rows[1].amazon_order_id == "A-1"
    assert rows[2].result.classification == Classification.PERSONAL
    assert rows[3].result.classification == Classification.REVIEW


# --- CLI ---


def test_cli_classify_and_review(tmp_path, monkeypatch, capsys):
    from src.statements.cli import main

    stmt = tmp_path / "stmt.csv"
    stmt.write_bytes(MF_SAMPLE.encode("utf-8"))
    rules_p = tmp_path / "rules.yaml"
    rules_p.write_text(
        "rules:\n"
        "  - name: cafe\n"
        "    classification: PERSONAL\n"
        "    reason: cafe personal\n"
        "    match:\n"
        "      description_keywords: [Cafe]\n",
        encoding="utf-8",
    )
    dec_p = tmp_path / "dec.json"
    outdir = tmp_path / "reports"
    rc = main(
        [
            "classify",
            "--month",
            "2026-10",
            "--statement-csv",
            str(stmt),
            "--rules",
            str(rules_p),
            "--decisions",
            str(dec_p),
            "--output-dir",
            str(outdir),
            "--format",
            "both",
        ]
    )
    assert rc == 0
    assert (outdir / "2026-10.csv").exists()
    assert (outdir / "2026-10.md").exists()

    # review with injected input via monkeypatched input
    from src.statements.cli import review_rows

    rows = classify_month(
        [Transaction(date=date(2026, 10, 5), description="Mystery Shop", amount=500)],
        [],
        DecisionStore(dec_p),
        [],
        None,
    )
    assert rows[0].result.classification == Classification.REVIEW
    answers = iter(["BUSINESS", "test reason"])
    saved = review_rows(
        rows,
        input_fn=lambda _p: next(answers),
        print_fn=lambda _m: None,
        store=DecisionStore(dec_p),
    )
    assert saved == 1
    assert DecisionStore(dec_p).lookup("Mystery Shop") is not None


def test_cli_decide_list(tmp_path, capsys):
    from src.statements.cli import main

    dec_p = tmp_path / "dec2.json"
    rc = main(
        [
            "decide",
            "--description",
            "Example Shop",
            "--classification",
            "BUSINESS",
            "--reason",
            "biz use",
            "--decisions",
            str(dec_p),
        ]
    )
    assert rc == 0
    assert json.loads(dec_p.read_text(encoding="utf-8"))


# --- MCP tools ---


@pytest.mark.asyncio
async def test_mcp_tools_with_mocked_scraper(tmp_path, monkeypatch):
    from src.tools import statements as stmt_tools

    monkeypatch.setenv("CARD_RULES_PATH", str(tmp_path / "norules.yaml"))
    monkeypatch.setenv("CARD_DECISIONS_PATH", str(tmp_path / "dec.json"))
    monkeypatch.setenv("CARD_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv("AMAZON_ORDERS_CSV", raising=False)
    monkeypatch.delenv("RECEIPTS_ROOT", raising=False)

    scraper = AsyncMock()
    scraper.list_financial_institutions.return_value = [{"name": "Example Card"}]
    scraper.get_monthly_transactions.return_value = [
        {
            "date": "2026/10/05",
            "description": "Example Store",
            "amount": -1500,
            "institution": "Example Card",
            "major_category": "食費",
            "minor_category": "",
            "memo": "",
            "is_transfer": False,
            "mf_id": "1",
        }
    ]
    cache = AsyncMock()
    cache.get.return_value = None

    r1 = await stmt_tools.list_financial_institutions(scraper, cache)
    assert r1["status"] == "success"
    r2 = await stmt_tools.list_monthly_transactions(scraper, cache, 2026, 10, None)
    assert r2["status"] == "success"
    r3 = await stmt_tools.classify_card_statement(scraper, cache, 2026, 10, None)
    assert r3["status"] == "success"
    assert r3["data"]["count"] == 1
    r4 = await stmt_tools.record_classification_decision("Example Store", "PERSONAL", "test", None)
    assert r4["status"] == "success"
    r5 = await stmt_tools.list_classification_decisions()
    assert r5["status"] == "success"
    assert r5["data"]["count"] >= 1


# --- FIX1 round 1 ---


def test_example_category_alone_prorated():
    example = Path("card_rules.yaml.example")
    rules = load_rules(example)

    def classify(desc: str, major: str = "") -> Classification:
        return apply_rules(_txn(desc, major_category=major), rules).classification

    # Category alone (no keyword) must be enough.
    assert classify("Something Else", "通信費") == Classification.PRORATED
    assert classify("Something Else", "水道・光熱費") == Classification.PRORATED
    # Keyword fallbacks still work without category.
    assert classify("携帯料金", "") == Classification.PRORATED
    assert classify("東京電力", "") == Classification.PRORATED


def test_note_service_personal_and_notebook_excluded():
    example = Path("card_rules.yaml.example")
    rules = load_rules(example)

    def classify(desc: str) -> Classification:
        return apply_rules(_txn(desc), rules).classification

    assert classify("NOTE*PREMIUM") == Classification.PERSONAL
    assert classify("note株式会社") == Classification.PERSONAL
    # notebook must NOT be PERSONAL via the note rule
    res = apply_rules(_txn("NOTEBOOK STAND"), rules)
    assert res.classification != Classification.PERSONAL


def test_aws_safer_and_web_services_not_amazon():
    example = Path("card_rules.yaml.example")
    rules = load_rules(example)
    assert apply_rules(_txn("AWS Bill"), rules).classification == Classification.BUSINESS
    assert apply_rules(_txn("AMAZON WEB SERVICES"), rules).classification == Classification.BUSINESS
    # Bare substring should not trigger AWS business rule.
    assert apply_rules(_txn("AWSSOME Service"), rules).classification == Classification.REVIEW
    # AWS bills are not Amazon retail.
    assert not is_amazon_description("AMAZON WEB SERVICES")
    assert not is_amazon_description("Amazon Web Services")
    assert is_amazon_description("AMAZON.CO.JP")


def test_remember_milk_no_bare_rtm():
    example = Path("card_rules.yaml.example")
    rules = load_rules(example)
    remember = [r for r in rules if "Remember" in r.name]
    assert remember
    r = remember[0]
    assert r.matches(_txn("Remember The Milk Pro"))
    assert r.matches(_txn("REMEMBERTHEMILK Pro"))
    assert not r.matches(_txn("RTM gadget"))


def test_amazon_decision_isolation(tmp_path):
    from src.statements.amazon import AmazonOrder

    dec_path = tmp_path / "dec.json"
    store = DecisionStore(dec_path)
    store.record(
        "AMAZON.CO.JP",
        "BUSINESS",
        "paper",
        None,
        amazon_item_title="プリンタ用紙 A4",
    )
    orders_same = [AmazonOrder(date(2026, 10, 4), "A-1", 1500, "プリンタ用紙 A4")]
    orders_diff = [AmazonOrder(date(2026, 10, 4), "A-2", 1500, "文具セット")]
    txn = Transaction(
        date=date(2026, 10, 5),
        description="AMAZON.CO.JP",
        amount=1500,
        institution="Example Card",
    )
    rows_same = classify_month([txn], [], store, orders_same, None)
    assert rows_same[0].result.source == "decision"
    assert rows_same[0].result.classification == Classification.BUSINESS
    assert rows_same[0].decision_key == amazon_decision_key("プリンタ用紙 A4")
    rows_diff = classify_month([txn], [], store, orders_diff, None)
    assert rows_diff[0].result.classification == Classification.REVIEW
    assert rows_diff[0].result.source != "decision"
    # Merchant-level non-Amazon decision still applies.
    store.record("Example Cafe 123", "PERSONAL", "human", None)
    rows_m = classify_month([_txn("Example Cafe 999")], [], store, [], None)
    assert rows_m[0].result.source == "decision"
    assert rows_m[0].result.classification == Classification.PERSONAL


def test_amazon_merchant_decision_blocked_without_flag(tmp_path):
    import pytest as _pytest

    store = DecisionStore(tmp_path / "d.json")
    with _pytest.raises(ValueError):
        store.record("AMAZON.CO.JP", "PERSONAL", "x", None)
    # lookup without item title must not return merchant blanket
    assert store.lookup("AMAZON.CO.JP") is None
    # explicitly merchant-wide is allowed and applied
    key = store.record("AMAZON.CO.JP", "PERSONAL", "x", None, merchant_wide=True)
    assert key
    assert store.lookup("AMAZON.CO.JP") is not None


def test_ambiguous_reason_includes_ids():
    from src.statements.amazon import AmazonOrder

    orders = [
        AmazonOrder(date(2026, 10, 4), "A-1", 1500, "Item One"),
        AmazonOrder(date(2026, 10, 5), "A-2", 1500, "Item Two"),
    ]
    txn = Transaction(
        date=date(2026, 10, 5),
        description="AMAZON",
        amount=1500,
        institution="Example Card",
    )
    rows = classify_month([txn], [], None, orders, None)
    assert rows[0].result.classification == Classification.REVIEW
    assert "A-1" in rows[0].result.reason
    assert "A-2" in rows[0].result.reason


def test_decision_key_in_csv_md(tmp_path):
    rows = [
        ClassifiedRow(
            transaction=_txn("Shop A", 1000),
            result=ClassificationResult(Classification.BUSINESS, "biz"),
            receipt_status="missing",
            decision_key="shop-a",
        ),
    ]
    csv_path = tmp_path / "r.csv"
    md_path = tmp_path / "r.md"
    write_csv(rows, csv_path)
    assert "decision_key" in csv_path.read_text(encoding="utf-8-sig")
    assert "shop-a" in csv_path.read_text(encoding="utf-8-sig")
    write_markdown(rows, 2026, 10, md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "shop-a" in md
    assert "判定キー" in md
