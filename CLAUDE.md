# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the price monitor (polls every 60 min, runs forever)
python monitor.py

# Run the dashboard
streamlit run app.py
# Then open http://localhost:8501
```

## Architecture

Two independent scripts that share `prices.json` as their data contract:

**[monitor.py](monitor.py)** — headless polling daemon
- Runs an infinite loop with `time.sleep(CHECK_INTERVAL_MINS * 60)` between checks
- For each destination: calls Skyscanner API → appends record to `prices.json` → checks if Error Fare
- Error Fare detection uses `rolling_average()` on all-but-the-latest records; fires only after `MIN_HISTORY` (5) data points exist
- Alert threshold: `price / avg <= ERROR_FARE_CUTOFF` (0.60 means 40%+ below average)
- Sends HTML-formatted Telegram message with a deep-link booking URL on detection
- Logs to both stdout and `monitor.log`

**[app.py](app.py)** — Streamlit read-only dashboard
- Reads `prices.json` with a 60-second `@st.cache_data` TTL
- Renders one section per city: 4 KPI metrics + Plotly line chart + expandable data table
- The sidebar "Refresh Data" button calls `st.cache_data.clear()` to force an immediate re-read
- Dashboard does its own error fare calculation from the DataFrame (independent of monitor.py logic)

**[prices.json](prices.json)** — shared state (auto-created by monitor.py)
```json
{
  "Tokyo":  [{"timestamp": "...", "date": "...", "price": 850.0, "route": "JFK→NRT", "depart_date": "..."}],
  "Berlin": [...]
}
```

## Configuration

All secrets and the origin airport live in `.env`:
```
RAPIDAPI_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ORIGIN_AIRPORT=JFK
```

Key constants in [monitor.py](monitor.py) (lines 35–53) that control behavior:
- `ERROR_FARE_CUTOFF = 0.60` — alert threshold (price must be < 60% of average)
- `CHECK_INTERVAL_MINS = 60` — polling frequency
- `DESTINATIONS` dict — add/remove cities and IATA codes here
- `DAYS_AHEAD = 60` — how far ahead to search for flights
- `MIN_HISTORY = 5` — data points required before alerts can fire

The same `ERROR_FARE_CUTOFF` constant is duplicated in [app.py](app.py) (line 29) and must be kept in sync manually if changed.
