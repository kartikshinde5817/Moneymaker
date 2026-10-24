"""Configuration. Everything comes from environment / .env, with safe defaults.

Secrets live here and never in the database in plaintext. Operational settings
that an admin should be able to change at runtime live in the `settings` table
instead (see app.services.settings_store).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- core -------------------------------------------------------------
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    # SQLite by default so the app runs with no infrastructure. Point this at
    # postgresql+psycopg://... to move to Postgres; nothing else changes.
    database_url: str = f"sqlite:///{BASE_DIR / 'moneymaker.db'}"
    secret_key: str = "dev-only-change-me"
    admin_password: str = ""  # blank = no login required (localhost use)
    # The platform owner's login. Customers sign up themselves.
    admin_email: str = "admin@local"
    platform_name: str = "Money Maker"
    support_email: str = ""

    # --- payments (Razorpay) ----------------------------------------------
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    trial_hours: int = 24

    # --- AI ---------------------------------------------------------------
    # Free by default: the paid Claude API is used only when AI_ENABLED=true is
    # set on purpose, so a leftover key can never cause charges by itself.
    ai_enabled: bool = False
    anthropic_api_key: str = ""
    llm_model_fast: str = "claude-haiku-4-5-20251001"
    llm_model_smart: str = "claude-sonnet-5"
    llm_daily_cost_cap_usd: float = 25.0

    # --- discovery sources ------------------------------------------------
    google_places_key: str = ""
    companies_house_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    serper_api_key: str = ""
    yelp_api_key: str = ""
    sam_gov_api_key: str = ""
    github_token: str = ""

    # --- email ------------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_use_ssl: bool = False
    # IMAP, for detecting replies. Same mailbox as SMTP on most providers.
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    imap_folder: str = "INBOX"

    # --- alerts -----------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- WhatsApp ---------------------------------------------------------
    # Cold WhatsApp to scraped numbers cannot be automated: Meta requires
    # prior opt-in and pre-approved templates, and unsolicited sending gets
    # the number banned. The app therefore builds a click-to-send queue a
    # human opens, rather than sending on its own.
    whatsapp_enabled: bool = True

    # A lead with neither an email address nor a phone number cannot be acted
    # on, so by default it is not stored at all. Set false to keep them for
    # research.
    require_contact: bool = True

    # --- sender identity (CAN-SPAM requires a real postal address) --------
    company_name: str = ""
    company_website: str = "https://www.atkind.com/"
    company_postal_address: str = ""
    sender_name: str = ""
    sender_email: str = ""
    portfolio_url: str = ""
    privacy_policy_url: str = ""
    public_base_url: str = "http://127.0.0.1:8000"

    # --- limits -----------------------------------------------------------
    daily_email_cap_global: int = 100
    # Render's free plan blocks outgoing SMTP. Set SEND_ON_SERVER=false there:
    # approving then only queues an email, and `python run.py --send-only` on
    # your own computer sends the queue through your mailbox.
    send_on_server: bool = True
    per_mailbox_daily_cap: int = 40
    cooling_off_days: int = 90
    min_score_to_email: int = 55
    scraper_user_agent: str = (
        "MoneyMakerBot/1.0 (+https://example.com/bot; lead research)"
    )
    fetch_timeout_seconds: int = 12
    max_fetch_bytes: int = 5_000_000

    # --- scheduler --------------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_timezone: str = "Asia/Kolkata"
    # New cities to rotate into each campaign per day.
    cities_per_day: int = 2

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def imap_ready(self) -> bool:
        return bool(self.imap_host and (self.imap_user or self.smtp_user))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Legacy env names from the old single-file app, so an existing .env keeps working.
for _legacy, _new in (
    ("GOOGLE_PLACES_KEY", "google_places_key"),
    ("ANTHROPIC_KEY", "anthropic_api_key"),
    ("SMTP_HOST", "smtp_host"),
    ("SMTP_USER", "smtp_user"),
    ("SMTP_PASS", "smtp_pass"),
):
    _v = os.environ.get(_legacy)
    if _v and not getattr(settings, _new, ""):
        setattr(settings, _new, _v)
