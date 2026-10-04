"""
Play Store Price Drop Tracker

Runs as a scheduled job (GitHub Actions). For each app in apps.json it:
  1. Fetches the current base price from the Play Store.
  2. Compares it with the last price stored in prices.db (SQLite).
  3. Sends a Telegram message if the price dropped.
  4. Saves the new price so the next run can compare against it.

On the very first run for an app, the price is only stored (no alert).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from google_play_scraper import app as play_app
from google_play_scraper.exceptions import GooglePlayScraperException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
APPS_FILE = BASE_DIR / "apps.json"
DB_FILE = BASE_DIR / "prices.db"

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 15  # seconds

# Currency symbols for nicer messages. Falls back to the currency code.
CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("price-tracker")


# ---------------------------------------------------------------------------
# Loading the app list
# ---------------------------------------------------------------------------

def load_apps(path: Path = APPS_FILE) -> list[dict]:
    """Read and validate apps.json. Returns a list of {package_id, country} dicts."""
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log.error("apps.json not found at %s", path)
        return []
    except json.JSONDecodeError as exc:
        log.error("apps.json is malformed: %s", exc)
        return []

    if not isinstance(data, list):
        log.error("apps.json must contain a JSON list of app objects.")
        return []

    valid_apps = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            log.error("apps.json entry #%d is not an object; skipping.", index)
            continue

        package_id = str(item.get("package_id", "")).strip()
        country = str(item.get("country", "")).strip().lower()

        if not package_id:
            log.error("apps.json entry #%d has no package_id; skipping.", index)
            continue
        if len(country) != 2:
            log.error(
                "apps.json entry '%s' has invalid country '%s'; skipping.",
                package_id, country,
            )
            continue

        valid_apps.append({"package_id": package_id, "country": country})

    log.info("Loaded %d app(s) from apps.json", len(valid_apps))
    return valid_apps


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    """Open a connection to prices.db (the file is created if it doesn't exist)."""
    return sqlite3.connect(DB_FILE)


def initialize_database() -> None:
    """Create the prices table if it doesn't exist yet."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prices (
                package_id     TEXT NOT NULL,
                country        TEXT NOT NULL,
                app_name       TEXT,
                currency       TEXT,
                current_price  REAL NOT NULL,
                previous_price REAL,
                last_checked   TEXT NOT NULL,
                last_changed   TEXT,
                PRIMARY KEY (package_id, country)
            )
            """
        )
    log.info("Database ready at %s", DB_FILE)


def get_stored_row(package_id: str, country: str) -> dict | None:
    """Return the stored row for an app/country, or None if it's new."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM prices WHERE package_id = ? AND country = ?",
            (package_id, country),
        ).fetchone()
    return dict(row) if row else None


def save_price(
    package_id: str,
    country: str,
    app_name: str,
    currency: str,
    new_price: float,
) -> None:
    """
    Insert or update the price for an app.

    - previous_price is set to the price we had before this run.
    - last_changed is only updated when the price actually changed.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = get_stored_row(package_id, country)

    with get_connection() as conn:
        if existing is None:
            # First time we see this app: establish the baseline.
            conn.execute(
                """
                INSERT INTO prices
                    (package_id, country, app_name, currency,
                     current_price, previous_price, last_checked, last_changed)
                VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (package_id, country, app_name, currency, new_price, now, now),
            )
        else:
            old_price = existing["current_price"]
            changed = abs(old_price - new_price) > 1e-9
            conn.execute(
                """
                UPDATE prices
                SET app_name       = ?,
                    currency       = ?,
                    previous_price = ?,
                    current_price  = ?,
                    last_checked   = ?,
                    last_changed   = CASE WHEN ? THEN ? ELSE last_changed END
                WHERE package_id = ? AND country = ?
                """,
                (
                    app_name, currency,
                    old_price, new_price,
                    now,
                    1 if changed else 0, now,
                    package_id, country,
                ),
            )


# ---------------------------------------------------------------------------
# Fetching prices
# ---------------------------------------------------------------------------

def get_price(package_id: str, country: str) -> dict | None:
    """
    Fetch the base price of an app from the Play Store.

    Returns {"name", "price", "currency"} or None on failure.
    Free apps return price 0.0. In-app purchase prices are never used,
    because the base price is the top-level 'price' field only.
    """
    try:
        info = play_app(package_id, lang="en", country=country)
    except GooglePlayScraperException as exc:
        # Covers "app not found" and most Play Store-side errors.
        log.error("[%s/%s] Play Store error: %s", package_id, country, exc)
        return None
    except Exception as exc:  # network timeouts, unexpected library errors
        log.error("[%s/%s] Unexpected error fetching price: %s", package_id, country, exc)
        return None

    price_raw = info.get("price")
    # The library returns 0 for free apps; treat None as free too.
    price = float(price_raw) if price_raw is not None else 0.0

    return {
        "name": info.get("title") or package_id,
        "price": price,
        "currency": info.get("currency") or "",
    }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def format_money(amount: float, currency: str) -> str:
    """Format a number with the currency symbol when known."""
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} " if currency else "")
    return f"{symbol}{amount:,.2f}"


def send_telegram_notification(text: str) -> bool:
    """Send a message via the Telegram Bot API. Returns True on success."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.error(
            "Telegram credentials missing. Set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID (in .env locally or as GitHub Secrets)."
        )
        return False

    try:
        response = requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)
        return False

    if response.status_code != 200:
        log.error("Telegram API error %s: %s", response.status_code, response.text)
        return False

    return True


def build_drop_message(
    app_name: str,
    package_id: str,
    country: str,
    currency: str,
    old_price: float,
    new_price: float,
) -> str:
    """Create the text of the price-drop notification."""
    savings = old_price - new_price
    return (
        "💰 Play Store Price Drop\n\n"
        f"App: {app_name} ({package_id})\n"
        f"Previous price: {format_money(old_price, currency)}\n"
        f"New price: {format_money(new_price, currency)}\n"
        f"You save: {format_money(savings, currency)}\n"
        f"Country: {country.upper()}"
    )


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def process_app(app: dict) -> str:
    """
    Check one app. Returns a status string for the summary:
    'baseline', 'dropped', 'unchanged', 'increased', 'notify_failed', or 'error'.
    """
    package_id = app["package_id"]
    country = app["country"]

    fetched = get_price(package_id, country)
    if fetched is None:
        return "error"

    new_price = fetched["price"]
    currency = fetched["currency"]
    name = fetched["name"]

    stored = get_stored_row(package_id, country)

    # First run for this app: store the baseline, do not notify.
    if stored is None:
        save_price(package_id, country, name, currency, new_price)
        log.info("[%s/%s] Baseline stored: %s", package_id, country,
                 format_money(new_price, currency))
        return "baseline"

    old_price = stored["current_price"]

    if new_price < old_price:
        message = build_drop_message(
            name, package_id, country, currency, old_price, new_price
        )
        sent = send_telegram_notification(message)
        # Save the new price regardless, so we don't re-alert on the same drop.
        save_price(package_id, country, name, currency, new_price)
        log.info("[%s/%s] Price dropped %s -> %s (notification %s)",
                 package_id, country,
                 format_money(old_price, currency),
                 format_money(new_price, currency),
                 "sent" if sent else "FAILED")
        return "dropped" if sent else "notify_failed"

    save_price(package_id, country, name, currency, new_price)
    if new_price > old_price:
        log.info("[%s/%s] Price increased; no notification.", package_id, country)
        return "increased"
    log.info("[%s/%s] Price unchanged.", package_id, country)
    return "unchanged"


def main() -> int:
    """Entry point. Returns a process exit code."""
    load_dotenv(BASE_DIR / ".env")  # no-op on GitHub Actions

    apps = load_apps()
    if not apps:
        log.error("No valid apps to track. Check apps.json.")
        return 1

    try:
        initialize_database()
    except sqlite3.Error as exc:
        log.error("Database error: %s", exc)
        return 1

    counts: dict[str, int] = {}
    for app in apps:
        try:
            status = process_app(app)
        except sqlite3.Error as exc:
            log.error("[%s/%s] Database error: %s", app["package_id"], app["country"], exc)
            status = "error"
        counts[status] = counts.get(status, 0) + 1

    log.info("Run summary: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # Exit non-zero only if every app failed, so one bad app doesn't fail the job.
    if counts.get("error", 0) == len(apps):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
