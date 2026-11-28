"""Application configuration management using Pydantic Settings.

This module provides a centralized configuration class that loads settings
from environment variables (.env file). All sensitive values are handled
securely using Pydantic's SecretStr type.
"""

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All settings are loaded from the .env file or environment variables.
    Sensitive values (passwords, tokens) are wrapped in SecretStr to prevent
    accidental logging or exposure.
    """

    # MoneyForward Authentication
    mf_email: str = Field(..., description="MoneyForward login email address")
    mf_password: SecretStr = Field(..., description="MoneyForward login password")
    mf_totp_secret: SecretStr = Field(default=SecretStr(""), description="Unused legacy field")

    # MCP Server Configuration
    mcp_host: str = Field(default="0.0.0.0", description="MCP server host to bind to")
    mcp_port: int = Field(default=8000, description="MCP server port")
    mcp_auth_token: SecretStr | None = Field(
        default=None, description="Optional Bearer token for MCP client authentication"
    )

    # Cache Configuration
    cache_ttl_seconds: int = Field(
        default=300, description="Cache TTL in seconds (default: 5 minutes)"
    )
    snapshot_interval_hours: int = Field(
        default=24, description="Asset snapshot interval in hours (default: 24 hours)"
    )
    cache_db_path: str = Field(
        default="/app/data/cache.db", description="SQLite cache database path"
    )

    # Browser Configuration
    browser_context_dir: str = Field(
        default="/app/browser-context",
        description="Directory for Playwright persistent context (cookies/sessions)",
    )
    browser_headless: bool = Field(default=True, description="Run browser in headless mode")

    # Logging Configuration
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    log_format: str = Field(default="json", description="Log output format (json or console)")

    # Account Configuration
    accounts_config_path: str = Field(
        default="accounts.yaml",
        description="Path to manual accounts YAML configuration file",
    )

    # Selector Configuration
    selectors_path: str = Field(
        default="src/selectors.yaml",
        description="Path to CSS selectors YAML configuration file",
    )

    # Card statement classification
    card_rules_path: str = Field(
        default="~/.config/moneyforward-mcp/card_rules.yaml",
        description="Path to card classification rules YAML file",
    )
    card_decisions_path: str = Field(
        default="~/.config/moneyforward-mcp/decisions.json",
        description="Path to human decision JSON store",
    )
    amazon_orders_csv: str | None = Field(
        default=None,
        description="Default path to Amazon order history CSV",
    )
    receipts_root: str | None = Field(
        default=None,
        description="Root dir for synced receipts (Google Drive for desktop)",
    )
    google_drive_access_token: SecretStr | None = Field(
        default=None,
        description="OAuth access token for Google Drive API v3",
    )
    google_drive_root_folder_id: str | None = Field(
        default=None,
        description="Optional Drive folder id for receipt search root",
    )
    card_report_dir: str = Field(
        default="~/.local/share/moneyforward-mcp/reports",
        description="Output dir for card statement reports",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def load_accounts(config_path: str | None = None) -> list[dict[str, Any]]:
    """Load manual account configurations from accounts.yaml.

    Args:
        config_path: Path to accounts YAML file. If None, uses settings default.

    Returns:
        List of account dictionaries with name, type, currency, mf_display_name.

    Raises:
        FileNotFoundError: If accounts.yaml does not exist.
    """
    from typing import cast

    path = Path(config_path) if config_path else Path(settings.accounts_config_path)
    if not path.exists():
        raise FileNotFoundError(f"Accounts config not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = cast(dict[str, Any], yaml.safe_load(f))

    accounts = data.get("accounts", [])
    return cast(list[dict[str, Any]], accounts)


# Singleton instance - import this to access settings throughout the application
settings = Settings()  # type: ignore[call-arg]
