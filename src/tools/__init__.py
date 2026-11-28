"""MCP tools module for MoneyForward ME data access.

This module provides FastMCP tools for accessing MoneyForward ME data including:
- Total assets
- Recent transactions
- Budget status
- Account refresh
- Health check
"""

from src.tools.assets import get_total_assets
from src.tools.budget import get_budget_status
from src.tools.common import build_error_response, build_success_response
from src.tools.health import health_check
from src.tools.manual_accounts import list_manual_accounts, update_manual_account
from src.tools.refresh import trigger_refresh
from src.tools.statements import (
    classify_card_statement,
    list_classification_decisions,
    list_financial_institutions,
    list_monthly_transactions,
    record_classification_decision,
)
from src.tools.transactions import list_recent_transactions

__all__ = [
    "build_error_response",
    "build_success_response",
    "classify_card_statement",
    "get_budget_status",
    "get_total_assets",
    "health_check",
    "list_classification_decisions",
    "list_financial_institutions",
    "list_manual_accounts",
    "list_monthly_transactions",
    "list_recent_transactions",
    "record_classification_decision",
    "trigger_refresh",
    "update_manual_account",
]
