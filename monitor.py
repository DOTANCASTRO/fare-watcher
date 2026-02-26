"""
monitor.py – Fare Watcher
=========================
Checks for "Error Fares" to Tokyo and Berlin using the Amadeus API.
An Error Fare = any price that is 40% or more below the recent average.
When one is found, a Telegram alert is sent with a direct booking link.

HOW TO RUN:
    python monitor.py

The script runs forever, checking prices every hour. Press Ctrl+C to stop.
All activity is also written to monitor.log in this folder.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Load your keys from the .env file ────────────────────────────────────────
load_dotenv()

AMADEUS_API_KEY   = os.getenv("AMADEUS_API_KEY")
AMADEUS_API_SECRET = os.getenv("AMADEUS_API_SECRET")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
ORIGIN_AIRPORT    = os.getenv("ORIGIN_AIRPORT", "JFK")

# ── Settings you can tweak ────────────────────────────────────────────────────
# How many % below average counts as an "Error Fare" (0.60 = 40% cheaper)
ERROR_FARE_CUTOFF   = 0.60

# How many minutes between each automatic check
CHECK_INTERVAL_MINS = 60

# File where price history is saved between runs
PRICES_FILE = "prices.json"

# Destinations: display name → IATA airport code
DESTINATIONS = {
    "Tokyo":  "NRT",   # Narita International (use HND for Haneda)
    "Berlin": "BER",   # Berlin Brandenburg Airport
}

# How many days ahead to look for flights
DAYS_AHEAD = 60

# Minimum number of data points before Error Fare detection kicks in
MIN_HISTORY = 5

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Price Storage ─────────────────────────────────────────────────────────────

def load_prices() -> dict:
    """Load saved price history from disk. Returns empty dict if none exists."""
    if Path(PRICES_FILE).exists():
        with open(PRICES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {city: [] for city in DESTINATIONS}


def save_prices(data: dict) -> None:
    """Save price history to disk."""
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Amadeus API ───────────────────────────────────────────────────────────────

def get_amadeus_token() -> str | None:
    """Get a short-lived access token from Amadeus (valid ~30 minutes)."""
    if not AMADEUS_API_KEY or not AMADEUS_API_SECRET:
        log.error("AMADEUS_API_KEY or AMADEUS_API_SECRET is not set in your .env file.")
        return None

    try:
        response = requests.post(
            "https://test.api.amadeus.com/v1/security/oauth2/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     AMADEUS_API_KEY,
                "client_secret": AMADEUS_API_SECRET,
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("access_token")
    except requests.exceptions.RequestException as e:
        log.error(f"Could not get Amadeus token: {e}")
        return None


def search_flights(origin: str, destination: str, depart_date: str, token: str) -> dict | None:
    """
    Call the Amadeus API and return details of the cheapest flight found.
    Returns a dict with price, carrier, and flight times, or None on failure.
    """
    try:
        response = requests.get(
            "https://test.api.amadeus.com/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "originLocationCode":      origin,
                "destinationLocationCode": destination,
                "departureDate":           depart_date,
                "adults":                  1,
                "currencyCode":            "USD",
                "max":                     10,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        log.error(f"HTTP error calling Amadeus API: {e}")
        return None
    except requests.exceptions.RequestException as e:
        log.error(f"Network error calling Amadeus API: {e}")
        return None

    offers = data.get("data", [])
    if not offers:
        log.warning(
            f"No flights returned for {origin}→{destination} on {depart_date}. "
            f"The route may not be available on this date."
        )
        return None

    # Find the cheapest offer
    valid = [o for o in offers if o.get("price", {}).get("total") is not None]
    if not valid:
        log.warning(f"Could not read price data for {origin}→{destination}.")
        log.debug(f"Raw API response: {json.dumps(data)[:500]}")
        return None

    cheapest_offer = min(valid, key=lambda o: float(o["price"]["total"]))
    price = float(cheapest_offer["price"]["total"])

    # Extract carrier and flight times from the first itinerary
    carriers = data.get("dictionaries", {}).get("carriers", {})
    first_seg = cheapest_offer["itineraries"][0]["segments"][0]
    last_seg  = cheapest_offer["itineraries"][-1]["segments"][-1]
    code      = first_seg.get("carrierCode", "")
    carrier   = carriers.get(code, code)
    flight_no = f"{code}{first_seg.get('number', '')}"
    departs_at = first_seg.get("departure", {}).get("at", depart_date)
    arrives_at = last_seg.get("arrival", {}).get("at", "")

    log.info(
        f"  {origin} → {destination}  |  {departs_at}  |  "
        f"{carrier} {flight_no}  |  ${price:.0f}"
    )
    return {
        "price":      price,
        "carrier":    carrier,
        "flight_no":  flight_no,
        "departs_at": departs_at,
        "arrives_at": arrives_at,
    }


def build_booking_link(origin: str, destination: str, depart_date: str) -> str:
    """Return a Skyscanner deep-link so the user can go straight to booking."""
    date_compact = depart_date.replace("-", "")
    return (
        f"https://www.skyscanner.com/transport/flights/"
        f"{origin.lower()}/{destination.lower()}/{date_compact}/"
    )


# ── Error Fare Logic ──────────────────────────────────────────────────────────

def rolling_average(records: list, last_n: int = 20) -> float | None:
    """Calculate the average price from the most recent N records."""
    recent_prices = [r["price"] for r in records[-last_n:]]
    return sum(recent_prices) / len(recent_prices) if recent_prices else None


def check_destination(city: str, iata: str, price_data: dict, token: str) -> None:
    """
    Fetch the current price for one destination, store it, and trigger
    a Telegram alert if it qualifies as an Error Fare.
    """
    depart_date = (datetime.now() + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    log.info(f"Checking {city} ({iata})  →  flight date {depart_date}")

    result = search_flights(ORIGIN_AIRPORT, iata, depart_date, token)
    if result is None:
        return  # Something went wrong; already logged above.

    price = result["price"]

    # Save this result to history
    record = {
        "timestamp":   datetime.now().isoformat(timespec="seconds"),
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "price":       price,
        "route":       f"{ORIGIN_AIRPORT}→{iata}",
        "depart_date": depart_date,
        "carrier":     result["carrier"],
        "flight_no":   result["flight_no"],
        "departs_at":  result["departs_at"],
        "arrives_at":  result["arrives_at"],
    }
    price_data[city].append(record)
    save_prices(price_data)

    history = price_data[city]

    # Wait until we have enough history to calculate a meaningful average
    if len(history) < MIN_HISTORY:
        log.info(
            f"  Still collecting baseline data for {city} "
            f"({len(history)}/{MIN_HISTORY} checks done). No alerts yet."
        )
        return

    # Use all history *except* the latest point to calculate the average
    average = rolling_average(history[:-1])
    if average is None:
        return

    ratio   = price / average
    pct_off = round((1 - ratio) * 100)
    log.info(f"  Current: ${price:.0f}  |  Average: ${average:.0f}  |  {pct_off}% vs avg")

    if ratio <= ERROR_FARE_CUTOFF:
        log.info(f"  *** ERROR FARE DETECTED for {city}! Sending Telegram alert. ***")
        booking_url = build_booking_link(ORIGIN_AIRPORT, iata, depart_date)
        send_telegram_alert(city, iata, price, average, pct_off, booking_url, depart_date)
    else:
        log.info(f"  No error fare. (Need price to drop below ${average * ERROR_FARE_CUTOFF:.0f})")


# ── Telegram Notifications ────────────────────────────────────────────────────

def send_telegram_alert(
    city: str,
    iata: str,
    price: float,
    average: float,
    pct_off: int,
    booking_url: str,
    depart_date: str,
) -> None:
    """Send a Telegram message with full fare details and a booking link."""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "your_telegram_bot_token_here":
        log.error("TELEGRAM_BOT_TOKEN is not set in your .env file.")
        return
    if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "your_telegram_chat_id_here":
        log.error("TELEGRAM_CHAT_ID is not set in your .env file.")
        return

    message = (
        f"🚨 <b>ERROR FARE ALERT — {city}!</b>\n\n"
        f"✈️  Route:   {ORIGIN_AIRPORT} → {iata}\n"
        f"📅  Depart:  {depart_date}\n"
        f"💰  Price:   <b>${price:.0f}</b>\n"
        f"📊  Average: ${average:.0f}\n"
        f"🔥  <b>{pct_off}% BELOW AVERAGE</b> — this is an Error Fare!\n\n"
        f"👉 <a href='{booking_url}'>Book now on Skyscanner</a>"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        r = requests.post(api_url, json=payload, timeout=10)
        if r.ok:
            log.info(f"  Telegram alert sent successfully for {city}.")
        else:
            log.error(f"  Telegram rejected the message: {r.status_code} – {r.text}")
    except requests.exceptions.RequestException as e:
        log.error(f"  Could not reach Telegram: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once() -> None:
    """Run a single round of checks across all destinations."""
    log.info("=" * 60)
    log.info("Starting fare check…")

    token = get_amadeus_token()
    if not token:
        log.error("Could not get Amadeus token — skipping this check.")
        return

    price_data = load_prices()

    # Make sure every destination has a list in the data file
    for city in DESTINATIONS:
        if city not in price_data:
            price_data[city] = []

    for city, iata in DESTINATIONS.items():
        check_destination(city, iata, price_data, token)

    log.info("Fare check complete.")


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Fare Watcher started. Press Ctrl+C to stop.")
    log.info(f"Destinations : {', '.join(DESTINATIONS.keys())}")
    log.info(f"Origin       : {ORIGIN_AIRPORT}")
    log.info(f"Check every  : {CHECK_INTERVAL_MINS} minutes")
    log.info(f"Error Fare   : price is 40%+ below the rolling average")
    log.info("=" * 60)

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log.info("Stopped by user. Goodbye!")
            break
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        log.info(f"Next check in {CHECK_INTERVAL_MINS} minutes. Sleeping…")
        time.sleep(CHECK_INTERVAL_MINS * 60)
