"""
OS Insider Scanner — Viewer Streamlit (v5.1)
Aggiunge colonne timestamp Filing in US ET, Italia (CEST), e età relativa.
Default sort: più recente in cima.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="OS Insider Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

DATA_FILE = Path("data.json")

st.markdown("""
<style>
    .stApp { background: #000000; }
    [data-testid="stHeader"] { background: transparent; }
    h1, h2, h3 { color: #f1f5f9; letter-spacing: -0.5px; }
    h1 { font-size: 24px !important; margin-bottom: 0; }
    .stMetric { background: #0f172a; padding: 10px; border-radius: 8px; border: 1px solid #1e293b; }
    [data-testid="stMetricValue"] { color: #f1f5f9; font-size: 20px; font-weight: 700; }
    [data-testid="stMetricLabel"] { color: #7aa8c8; font-size: 11px; text-transform: uppercase; }
    .stButton > button { background: #0099ff; color: white; border: 0; border-radius: 8px; font-weight: 600; padding: 10px 20px; }
    .stDataFrame { font-size: 12px; }
    [data-testid="stSidebar"] { background: #0f172a; }
</style>
""", unsafe_allow_html=True)


# ============================================================
# TIMEZONE HELPERS
# ============================================================

def parse_utc_timestamp(s):
    """Parse ISO timestamp con o senza timezone, ritorna datetime UTC-aware."""
    if not s:
        return None
    try:
        # Try ISO format
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def fmt_us_et(utc_dt):
    """Formatta UTC datetime in US Eastern Time (EDT/EST auto)."""
    if utc_dt is None:
        return "—"
    # Maggio = EDT (UTC-4). In inverno (Nov-Mar) sarebbe EST (UTC-5).
    # Approssimazione: se mese è 11,12,1,2 → EST, altrimenti EDT
    is_dst = utc_dt.month not in (11, 12, 1, 2, 3)
    offset = -4 if is_dst else -5
    et = utc_dt + timedelta(hours=offset)
    suffix = "ET" if is_dst else "EST"
    return f"{et.strftime('%d %b %H:%M')} {suffix}"


def fmt_italy(utc_dt):
    """Formatta UTC datetime in ora italiana (CEST/CET)."""
    if utc_dt is None:
        return "—"
    is_dst = utc_dt.month not in (11, 12, 1, 2)
    offset = 2 if is_dst else 1
    it = utc_dt + timedelta(hours=offset)
    return f"{it.strftime('%d %b %H:%M')}"


def fmt_age(utc_dt):
    """Età relativa: '2m', '1h 15m', '3h ago', '2d'."""
    if utc_dt is None:
        return "—"
    now = datetime.now(timezone.utc)
    delta = now - utc_dt
    secs = delta.total_seconds()
    if secs < 0:
        return "future?"
    if secs < 60:
        return f"{int(secs)}s"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    rem_min = mins % 60
    if hours < 24:
        if rem_min > 0:
            return f"{hours}h {rem_min}m"
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


# ============================================================
# DATA LOADING
# ============================================================

@st.cache_data(ttl=60)
def load_data():
    if not DATA_FILE.exists():
        return None, None
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        purchases = data.get("purchases", [])
        last_update = data.get("last_update")
        return purchases, last_update
    except (json.JSONDecodeError, ValueError):
        return None, None


# ============================================================
# UI
# ============================================================

st.title("📊 OS Insider Scanner")
st.caption("Form 4 P+A · Biotech + Semiconductors · GitHub Actions cron")

with st.sidebar:
    st.header("⚙️ Filtri")
    soglia = st.number_input("Soglia min USD", min_value=0, value=15000, step=1000)
    filter_clevel = st.checkbox("Solo C-level", value=False)
    sector_filter = st.multiselect(
        "Settori",
        ["Biotech", "Semiconductors"],
        default=["Biotech", "Semiconductors"]
    )
    sort_mode = st.selectbox(
        "Ordina per",
        ["Più recenti (filing)", "Valore decrescente", "Per ticker"],
        index=0,
    )
    
    st.divider()
    st.subheader("📊 Settori monitorati")
    st.caption("**Biotech**: 2836, 2834, 2833, 2835, 8731")
    st.caption("**Semiconductors**: 3674, 3670, 3571, 3572, 3576, 3577")
    
    st.divider()
    st.subheader("🕐 Fusi orari mostrati")
    st.caption("• **US ET** (Eastern): ora del mercato US")
    st.caption("• **IT**: ora italiana (CEST)")
    st.caption("• **Età**: tempo dal filing")
    
    st.divider()
    if st.button("🔄 Reload data.json", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

purchases, last_update = load_data()

if purchases is None:
    st.warning("⚠️ data.json non ancora generato. Aspetta che il fetcher GitHub Actions completi il primo run.")
    st.stop()

# Filtro 24h
cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=24)
recent = [p for p in purchases if parse_utc_timestamp(p.get("detected_at", "")) and parse_utc_timestamp(p.get("detected_at", "")) >= cutoff_utc]

# Applica filtri user
filtered = []
cLevel_keywords = ["ceo", "cfo", "coo", "president", "chief"]
for p in recent:
    if p["total"] < soglia:
        continue
    if p["sector"] not in sector_filter:
        continue
    if filter_clevel:
        t_low = (p.get("insider_title") or "").lower()
        if not any(k in t_low for k in cLevel_keywords):
            continue
    filtered.append(p)

# Sort
def filing_sort_key(p):
    dt = parse_utc_timestamp(p.get("filing_datetime_utc", "") or p.get("detected_at", ""))
    return dt or datetime(1970, 1, 1, tzinfo=timezone.utc)

if sort_mode == "Più recenti (filing)":
    filtered.sort(key=filing_sort_key, reverse=True)
elif sort_mode == "Per ticker":
    filtered.sort(key=lambda x: (x["ticker"], -x["total"]))
else:  # Valore decrescente
    filtered.sort(key=lambda x: -x["total"])

# Header status — ULTIMO FILING + ULTIMO REFRESH FETCHER
if filtered:
    most_recent_dt = filing_sort_key(filtered[0])
    age_str = fmt_age(most_recent_dt)
    st.success(f"🔥 Ultimo filing: **{filtered[0]['ticker']}** — {age_str} ago · {fmt_us_et(most_recent_dt)} · {fmt_italy(most_recent_dt)} IT")

if last_update:
    lu = parse_utc_timestamp(last_update)
    if lu:
        age_min = (datetime.now(timezone.utc) - lu).total_seconds() / 60
        if age_min < 5:
            st.success(f"✅ Fetcher aggiornato {age_min:.0f} min fa · {len(filtered)} filing visibili nei filtri")
        elif age_min < 15:
            st.info(f"ℹ️ Fetcher aggiornato {age_min:.0f} min fa")
        elif age_min < 60:
            st.warning(f"⚠️ Fetcher fermo da {age_min:.0f} min — possibile problema cron")
        else:
            st.error(f"🚨 Fetcher fermo da {int(age_min)} min!")

# Metrics
c1, c2, c3, c4 = st.columns(4)
with c1: st.metric("Filing 24h", len(filtered))
with c2:
    tv = sum(p["total"] for p in filtered)
    val_str = f"${tv/1e6:.2f}M" if tv >= 1e6 else (f"${tv/1e3:.1f}k" if tv >= 1e3 else f"${tv:,.0f}")
    st.metric("Valore tot", val_str)
with c3:
    cluster_tickers = set(p["ticker"] for p in filtered if p.get("is_cluster"))
    st.metric("Cluster", len(cluster_tickers))
with c4:
    biotech = sum(1 for p in filtered if p["sector"] == "Biotech")
    st.metric("Biotech", biotech)

st.divider()

if not filtered:
    st.info("📭 Nessun filing che corrisponde ai filtri attuali.")
else:
    df = pd.DataFrame(filtered)
    
    # Format columns
    df["💰 Tot"] = df["total"].apply(
        lambda x: f"${x/1e6:.2f}M" if x >= 1e6 else (f"${x/1e3:.1f}k" if x >= 1e3 else f"${x:,.0f}")
    )
    df["📊 Az."] = df["shares"].apply(lambda x: f"{x:,.0f}")
    df["💵 $"] = df["price"].apply(lambda x: f"${x:.2f}")
    df["🏢 Ticker"] = df.apply(
        lambda r: f"⚡{r['ticker']} ({int(r.get('cluster_size', 0))})" if r.get("is_cluster") else r["ticker"],
        axis=1
    )
    
    # Timestamp columns
    df["_filing_dt"] = df["filing_datetime_utc"].apply(parse_utc_timestamp) if "filing_datetime_utc" in df.columns else None
    df["🕐 US ET"] = df["_filing_dt"].apply(fmt_us_et) if "_filing_dt" in df.columns else "—"
    df["🇮🇹 IT"] = df["_filing_dt"].apply(fmt_italy) if "_filing_dt" in df.columns else "—"
    df["⏱ Età"] = df["_filing_dt"].apply(fmt_age) if "_filing_dt" in df.columns else "—"
    
    st.dataframe(
        df[["⏱ Età", "🕐 US ET", "🇮🇹 IT", "🏢 Ticker", "company", "insider_name", "insider_title",
            "tx_date", "📊 Az.", "💵 $", "💰 Tot", "sector", "xml_url"]],
        column_config={
            "company": "Azienda",
            "insider_name": "Insider",
            "insider_title": "Ruolo",
            "tx_date": "Tx Date",
            "sector": "Set.",
            "xml_url": st.column_config.LinkColumn("📄", display_text="link"),
        },
        hide_index=True,
        use_container_width=True,
    )
    
    # CSV export with timestamps in clean format
    df_export = df.copy()
    if "_filing_dt" in df_export.columns:
        df_export["filing_us_et"] = df_export["_filing_dt"].apply(fmt_us_et)
        df_export["filing_italy"] = df_export["_filing_dt"].apply(fmt_italy)
        df_export["age"] = df_export["_filing_dt"].apply(fmt_age)
        df_export = df_export.drop(columns=["_filing_dt"])
    
    csv = df_export.to_csv(index=False)
    st.download_button(
        "📥 Esporta CSV",
        csv,
        f"insider_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv",
        "text/csv",
    )

st.divider()
st.caption(
    f"💾 Storage: {len(purchases):,} purchases tot · "
    f"Refresh data.json: ogni 60s · "
    f"Fetcher cron: 2 min (mercato) - 30 min (notte) · v5.1"
)
