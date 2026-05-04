"""
OS Insider Scanner — Viewer Streamlit (v5.0)
Legge data.json prodotto da fetcher.py via GitHub Actions.
Zero chiamate a SEC dall'app: solo visualizzazione storica + filtri.
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


@st.cache_data(ttl=60)  # ricarica data.json ogni 60s
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


st.title("📊 OS Insider Scanner")
st.caption("Form 4 P+A · Biotech + Semiconductors · GitHub Actions cron 5min")

with st.sidebar:
    st.header("⚙️ Filtri")
    soglia = st.number_input("Soglia min USD", min_value=0, value=15000, step=1000)
    filter_clevel = st.checkbox("Solo C-level", value=False)
    sector_filter = st.multiselect(
        "Settori",
        ["Biotech", "Semiconductors"],
        default=["Biotech", "Semiconductors"]
    )
    sort_mode = st.selectbox("Ordina per", ["Valore decrescente", "Più recenti", "Per ticker"])
    
    st.divider()
    st.subheader("📊 Settori monitorati")
    st.caption("**Biotech**: 2836, 2834, 2833, 2835, 8731")
    st.caption("**Semiconductors**: 3674, 3670, 3571, 3572, 3576, 3577")
    
    st.divider()
    if st.button("🔄 Reload data.json", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

purchases, last_update = load_data()

if purchases is None:
    st.warning("⚠️ data.json non ancora generato. Aspetta che il fetcher GitHub Actions completi il primo run.")
    st.info("Per forzare ora: vai su GitHub repo → Actions → Run workflow manualmente.")
    st.stop()

# Filtro 24h
cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
recent = [p for p in purchases if p.get("detected_at", "") >= cutoff]

# Filtri user
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
if sort_mode == "Più recenti":
    filtered.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
elif sort_mode == "Per ticker":
    filtered.sort(key=lambda x: (x["ticker"], -x["total"]))
else:
    filtered.sort(key=lambda x: -x["total"])

# Header status
if last_update:
    try:
        lu = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - lu).total_seconds() / 60
        if age_min < 10:
            st.success(f"✅ Dati aggiornati {age_min:.0f} min fa · {len(filtered)} filing visibili")
        elif age_min < 30:
            st.info(f"ℹ️ Ultimo aggiornamento {age_min:.0f} min fa")
        else:
            st.warning(f"⚠️ Ultimo aggiornamento {age_min:.0f} min fa — fetcher potrebbe essere fermo")
    except (ValueError, AttributeError):
        st.info(f"Ultimo aggiornamento: {last_update}")

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
    st.info("📭 Nessun filing che corrisponde ai filtri attuali. Prova ad abbassare la soglia o aspetta il prossimo cron.")
else:
    df = pd.DataFrame(filtered)
    df["💰 Tot"] = df["total"].apply(
        lambda x: f"${x/1e6:.2f}M" if x >= 1e6 else (f"${x/1e3:.1f}k" if x >= 1e3 else f"${x:,.0f}")
    )
    df["📊 Az."] = df["shares"].apply(lambda x: f"{x:,.0f}")
    df["💵 $"] = df["price"].apply(lambda x: f"${x:.2f}")
    df["🏢 Ticker"] = df.apply(
        lambda r: f"⚡{r['ticker']} ({int(r.get('cluster_size', 0))})" if r.get("is_cluster") else r["ticker"],
        axis=1
    )
    
    st.dataframe(
        df[["🏢 Ticker", "company", "insider_name", "insider_title", "tx_date",
            "📊 Az.", "💵 $", "💰 Tot", "sector", "xml_url"]],
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
    
    csv = df.to_csv(index=False)
    st.download_button(
        "📥 Esporta CSV",
        csv,
        f"insider_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv",
        "text/csv",
    )

st.divider()
st.caption(
    f"💾 Storage: {len(purchases):,} purchases tot · "
    f"Ultimo refresh data.json: ogni 60s · "
    f"Fetcher cron: ogni 5 min · v5.0"
)
