"""
app.py – Fare Watcher Dashboard
================================
A Streamlit web app that shows price trends and highlights Error Fares.

HOW TO RUN:
    streamlit run app.py

Then open http://localhost:8501 in your browser.
The dashboard auto-refreshes whenever new data is available.
"""

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Fare Watcher",
    page_icon="✈️",
    layout="wide",
)

# ── Constants (must match monitor.py) ────────────────────────────────────────
PRICES_FILE       = "prices.json"
ERROR_FARE_CUTOFF = 0.60   # 40% below average

# ── Header ────────────────────────────────────────────────────────────────────
st.title("✈️ Fare Watcher Dashboard")
st.caption(
    "Monitoring Error Fares to Tokyo & Berlin (round trip) — "
    "an Error Fare is any price 40%+ below the recent average."
)
st.divider()

# ── Data loading ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)   # re-read the file at most once per minute
def load_data() -> dict:
    if not Path(PRICES_FILE).exists():
        return {}
    with open(PRICES_FILE, encoding="utf-8") as f:
        return json.load(f)


data = load_data()

# ── Empty state ───────────────────────────────────────────────────────────────
if not data or all(len(v) == 0 for v in data.values()):
    st.info(
        "No price data yet.\n\n"
        "Run **`python monitor.py`** in your terminal to start collecting prices. "
        "This dashboard will update automatically."
    )
    st.stop()

# ── One section per destination ───────────────────────────────────────────────
total_error_fares = 0
all_error_rows = []
for city, records in data.items():
    if not records:
        st.subheader(f"✈️ {city}")
        st.warning(f"No data collected for {city} yet.")
        st.divider()
        continue

    # Build a clean DataFrame
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Running (expanding) average — this is what the monitor uses
    df["running_avg"] = df["price"].expanding().mean()

    # Mark error fares: price < 60% of the average calculated *before* that point
    # For the first row there's no prior average, so it can't be an error fare.
    df["prev_avg"] = df["price"].shift(1).expanding().mean()
    df["is_error_fare"] = df["price"] < (df["prev_avg"] * ERROR_FARE_CUTOFF)
    df["is_error_fare"] = df["is_error_fare"].fillna(False)

    # Ensure optional columns exist (older records may not have them)
    for col in ["departs_at", "arrives_at", "return_date", "carrier", "flight_no"]:
        if col not in df.columns:
            df[col] = "—"
        else:
            df[col] = df[col].fillna("—")

    overall_avg = df["price"].mean()
    latest      = df.iloc[-1]
    pct_vs_avg  = ((latest["price"] - overall_avg) / overall_avg) * 100
    error_count = int(df["is_error_fare"].sum())
    total_error_fares += error_count

    # ── KPI cards ─────────────────────────────────────────────────────────────
    st.subheader(f"✈️ {city}")
    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        label="Latest Price",
        value=f"${latest['price']:.0f}",
        help="Most recently recorded price for this route.",
    )
    col2.metric(
        label="All-Time Average",
        value=f"${overall_avg:.0f}",
        delta=f"{pct_vs_avg:+.1f}% vs latest",
        delta_color="inverse",
        help="Average of every price recorded so far.",
    )
    col3.metric(
        label="Error Fare Threshold",
        value=f"${overall_avg * ERROR_FARE_CUTOFF:.0f}",
        help="Any price at or below this value triggers an alert.",
    )
    col4.metric(
        label="Error Fares Found",
        value=error_count,
        delta="🔥 deal spotted!" if error_count > 0 else "None yet",
        delta_color="off",
        help="Total number of Error Fares detected since monitoring started.",
    )

    # ── Price Trend Chart ─────────────────────────────────────────────────────
    fig = go.Figure()

    # Error fare shaded zone
    fig.add_hrect(
        y0=0,
        y1=overall_avg * ERROR_FARE_CUTOFF,
        fillcolor="rgba(220, 53, 69, 0.08)",
        line_width=0,
        annotation_text="Error Fare Zone",
        annotation_position="top left",
        annotation_font_color="rgba(220, 53, 69, 0.7)",
    )

    # Error fare threshold dashed line
    fig.add_hline(
        y=overall_avg * ERROR_FARE_CUTOFF,
        line_dash="dot",
        line_color="rgba(220, 53, 69, 0.6)",
        line_width=1.5,
    )

    # Actual price line
    carrier_col = df["carrier"] if "carrier" in df.columns else "—"
    fig.add_trace(go.Scatter(
        x=df["timestamp"],
        y=df["price"],
        mode="lines+markers",
        name="Recorded Price",
        line=dict(color="#4A90D9", width=2),
        marker=dict(size=6),
        customdata=df[["carrier", "flight_no", "departs_at"]].fillna("—") if "carrier" in df.columns else None,
        hovertemplate="$%{y:.0f}  |  %{customdata[0]} %{customdata[1]}<br>Departs: %{customdata[2]}<extra></extra>",
    ))

    # Running average line
    fig.add_trace(go.Scatter(
        x=df["timestamp"],
        y=df["running_avg"],
        mode="lines",
        name="Running Average",
        line=dict(color="#F0A500", width=2, dash="dash"),
        hovertemplate="avg $%{y:.0f}<extra></extra>",
    ))

    # Error fare highlight markers
    error_df = df[df["is_error_fare"]]

    # Collect error fares for the summary table
    for _, row in error_df.iterrows():
        pct_below   = round((1 - row["price"] / row["prev_avg"]) * 100)
        route_parts = str(row.get("route", "→")).split("→")
        origin_c    = route_parts[0].strip().lower() if len(route_parts) > 1 else ""
        dest_c      = route_parts[1].strip().lower() if len(route_parts) > 1 else ""
        dep_str     = str(row["depart_date"]).replace("-", "")
        ret_str     = str(row["return_date"]).replace("-", "")
        book_link   = (
            f"https://www.skyscanner.com/transport/flights/{origin_c}/{dest_c}/{dep_str}/{ret_str}/"
            if origin_c and dest_c and row["depart_date"] != "—" else ""
        )
        all_error_rows.append({
            "City":        city,
            "Found At":    row["timestamp"].strftime("%Y-%m-%d %H:%M"),
            "Price (USD)": f"${row['price']:.0f}",
            "Avg at Time": f"${row['prev_avg']:.0f}",
            "% Below Avg": f"{pct_below}%",
            "Departs":     row["depart_date"],
            "Returns":     row["return_date"],
            "Airline":     row["carrier"],
            "Flight":      row["flight_no"],
            "Book":        book_link,
        })

    if not error_df.empty:
        fig.add_trace(go.Scatter(
            x=error_df["timestamp"],
            y=error_df["price"],
            mode="markers",
            name="Error Fare!",
            marker=dict(color="#DC3545", size=16, symbol="star"),
            hovertemplate="ERROR FARE: $%{y:.0f}<extra></extra>",
        ))

    fig.update_layout(
        title=f"{city} – Price History (USD, round trip)",
        xaxis_title="Check Date & Time",
        yaxis_title="Price (USD)",
        legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="left", x=0),
        hovermode="x unified",
        height=420,
        plot_bgcolor="white",
        paper_bgcolor="white",
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0", rangemode="tozero"),
        margin=dict(l=0, r=0, t=40, b=0),
    )

    st.plotly_chart(fig, use_container_width=True)

    # ── Error Fare Callout ────────────────────────────────────────────────────
    if error_count > 0:
        best_deal = error_df.loc[error_df["price"].idxmin()]
        saving_pct = round((1 - best_deal["price"] / overall_avg) * 100)
        st.error(
            f"🔥 **Best Error Fare found:** ${best_deal['price']:.0f} "
            f"({saving_pct}% below average) on {best_deal['timestamp'].strftime('%Y-%m-%d %H:%M')}"
        )

    # ── Data Table ────────────────────────────────────────────────────────────
    with st.expander(f"📋 All recorded prices for {city} (most recent first)"):
        cols = ["timestamp", "price", "departs_at", "arrives_at", "return_date", "carrier", "flight_no", "running_avg", "is_error_fare"]
        display = df[cols].copy()
        display.columns = ["Checked At", "Price (USD)", "Departs", "Arrives", "Return Date", "Airline", "Flight", "Running Avg", "Error Fare?"]
        display["Checked At"]  = display["Checked At"].dt.strftime("%Y-%m-%d %H:%M")
        display["Price (USD)"] = display["Price (USD)"].map("${:.0f}".format)
        display["Running Avg"] = display["Running Avg"].map("${:.0f}".format)
        display["Error Fare?"] = display["Error Fare?"].map({True: "🔥 YES", False: "—"})
        st.dataframe(display.iloc[::-1].reset_index(drop=True), use_container_width=True)

    st.divider()

# ── Error Fares Summary ───────────────────────────────────────────────────────
if all_error_rows:
    st.header("🔥 All Error Fares Detected")
    ef_df = pd.DataFrame(all_error_rows)
    st.dataframe(
        ef_df,
        column_config={
            "Book": st.column_config.LinkColumn("Book on Skyscanner"),
        },
        use_container_width=True,
        hide_index=True,
    )
    st.divider()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.divider()
    last_ts = max((r["timestamp"] for v in data.values() for r in v), default=None)
    if last_ts:
        st.metric("Last Check", last_ts[:16].replace("T", " "))
    total_checks = sum(len(v) for v in data.values())
    st.metric("Total Price Checks", total_checks)
    st.metric("Total Error Fares", total_error_fares)
    st.divider()

    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption("Dashboard caches data for 60 seconds.")
