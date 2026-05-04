"""
OS Insider Scanner — Streamlit App (v3.4 DEBUG)
Mostra stats dettagliate del parser per capire dove fallisce
"""

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

import edgar

st.set_page_config(
    page_title="OS Insider Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

DB_PATH = Path("insider_scanner.db")
RETENTION_HOURS = 28

st.markdown("""
<style>
    .stApp { background: #000000; }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stToolbar"] { display: none; }
    h1, h2, h3 { color: #f1f5f9; letter-spacing: -0.5px; }
    h1 { font-size: 24px !important; margin-bottom: 0; }
    .stMetric { background: #0f172a; padding: 10px; border-radius: 8px; border: 1px solid #1e293b; }
    [data-testid="stMetricValue"] { color: #f1f5f9; font-size: 20px; font-weight: 700; }
    [data-testid="stMetricLabel"] { color: #7aa8c8; font-size: 11px; text-transform: uppercase; }
    .stButton > button { background: #0099ff; color: white; border: 0; border-radius: 8px; font-weight: 600; padding: 10px 20px; }
    .stButton > button:hover { background: #0077cc; }
    .stDataFrame { font-size: 12px; }
    [data-testid="stSidebar"] { background: #0f172a; }
    div[data-testid="stStatusWidget"] { display: none; }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sic_cache (cik TEXT PRIMARY KEY, sic TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS processed_filings (accession TEXT PRIMARY KEY, processed_at TEXT);
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            accession TEXT, ticker TEXT, company TEXT,
            insider_name TEXT, insider_title TEXT,
            tx_date TEXT, filing_date TEXT,
            shares REAL, price REAL, total REAL,
            sector TEXT, sic TEXT, xml_url TEXT, parser_strategy TEXT,
            is_cluster INTEGER DEFAULT 0, cluster_size INTEGER DEFAULT 0,
            detected_at TEXT, telegram_sent INTEGER DEFAULT 0,
            UNIQUE(accession, insider_name, tx_date, shares, price)
        );
        CREATE INDEX IF NOT EXISTS idx_p_detected ON purchases(detected_at);
    """)
    return conn


def db_get_cached_sic(conn, cik):
    cur = conn.execute("SELECT sic FROM sic_cache WHERE cik = ?", (cik,))
    row = cur.fetchone()
    return row[0] if row else None


def db_set_cached_sic(conn, cik, sic):
    conn.execute(
        "INSERT OR REPLACE INTO sic_cache (cik, sic, updated_at) VALUES (?, ?, ?)",
        (cik, sic or "", datetime.now(timezone.utc).isoformat())
    )


def db_is_processed(conn, accession):
    cur = conn.execute("SELECT 1 FROM processed_filings WHERE accession = ?", (accession,))
    return cur.fetchone() is not None


def db_mark_processed(conn, accession):
    conn.execute(
        "INSERT OR IGNORE INTO processed_filings (accession, processed_at) VALUES (?, ?)",
        (accession, datetime.now(timezone.utc).isoformat())
    )


def db_save_purchase(conn, p):
    try:
        conn.execute("""
            INSERT INTO purchases (accession, ticker, company, insider_name, insider_title,
                tx_date, filing_date, shares, price, total, sector, sic, xml_url,
                parser_strategy, is_cluster, cluster_size, detected_at, telegram_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            p["accession"], p["ticker"], p["company"], p["insider_name"], p["insider_title"],
            p["tx_date"], p["filing_date"], p["shares"], p["price"], p["total"],
            p["sector"], p["sic"], p["xml_url"], p["parser_strategy"],
            int(p.get("is_cluster", False)), p.get("cluster_size", 0),
            datetime.now(timezone.utc).isoformat(),
        ))
        return True
    except sqlite3.IntegrityError:
        return False


def db_load_recent(conn, hours=24):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return pd.read_sql_query(
        "SELECT * FROM purchases WHERE detected_at >= ? ORDER BY total DESC",
        conn, params=(cutoff,)
    )


def db_purge_old(conn, hours=RETENTION_HOURS):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn.execute("DELETE FROM purchases WHERE detected_at < ?", (cutoff,))
    conn.commit()


def db_stats(conn):
    cur = conn.execute("SELECT COUNT(*) FROM sic_cache WHERE sic != ''")
    sic = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM processed_filings")
    proc = cur.fetchone()[0]
    return {"sic_cache": sic, "processed": proc}


def get_telegram_config():
    try:
        return {
            "bot_token": st.secrets["TELEGRAM_BOT_TOKEN"],
            "chat_id": st.secrets["TELEGRAM_CHAT_ID"],
        }
    except Exception:
        return None


def get_email():
    try:
        return st.secrets["SEC_EMAIL"]
    except Exception:
        return "scanner@example.com"


def run_scan_full(progress_ph, log_ph, soglia, filter_clevel, parse_concurrency):
    conn = get_db()
    session = edgar.make_session(get_email())
    
    edgar.reset_debug_stats()
    
    log_lines = []
    def add_log(msg):
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        log_ph.text("\n".join(log_lines[-50:]))
    
    t0 = time.time()
    
    progress_ph.info("📥 Scarico full-index EDGAR…")
    target_dates = edgar.get_target_dates(2)
    all_filings = []
    for td in target_dates:
        filings = edgar.fetch_full_index(session, td)
        all_filings.extend(filings)
        add_log(f"Full-index {td['yyyymmdd']}: {len(filings)} Form 4")
    
    if not all_filings:
        return {"error": "Full-index non disponibile"}
    
    new_filings = [f for f in all_filings if not db_is_processed(conn, f["accession"])]
    add_log(f"Da analizzare: {len(new_filings)}")
    
    if not new_filings:
        return {"new_matches": 0, "elapsed": time.time() - t0, "msg": "Nessun nuovo filing"}
    
    candidates = [f for f in new_filings if edgar.is_candidate_company_name(f["company"])]
    add_log(f"Prefiltro nome: {len(candidates)}/{len(new_filings)} candidati")
    
    for f in new_filings:
        db_mark_processed(conn, f["accession"])
    conn.commit()
    
    if not candidates:
        return {"new_matches": 0, "elapsed": time.time() - t0, "msg": "Nessun candidato"}
    
    # SIC lookup
    unique_ciks = list(set(f["cik"] for f in candidates))
    sic_map = {}
    ciks_to_fetch = []
    for cik in unique_ciks:
        cached = db_get_cached_sic(conn, cik)
        if cached is not None:
            sic_map[cik] = cached if cached else None
        else:
            ciks_to_fetch.append(cik)
    add_log(f"Cache SIC: {len(unique_ciks) - len(ciks_to_fetch)}/{len(unique_ciks)} hit")
    
    if ciks_to_fetch:
        progress_bar = progress_ph.progress(0.0, text=f"SIC lookup 0/{len(ciks_to_fetch)}")
        for i, cik in enumerate(ciks_to_fetch, 1):
            sic = edgar.fetch_filer_sic(session, cik)
            db_set_cached_sic(conn, cik, sic)
            sic_map[cik] = sic
            if i % 5 == 0 or i == len(ciks_to_fetch):
                conn.commit()
                progress_bar.progress(i / len(ciks_to_fetch), text=f"SIC {i}/{len(ciks_to_fetch)}")
        progress_bar.empty()
    
    relevant = [f for f in candidates if sic_map.get(f["cik"]) and sic_map[f["cik"]] in edgar.SIC_WHITELIST]
    add_log(f"Settori target: {len(relevant)}/{len(candidates)}")
    
    if not relevant:
        return {"new_matches": 0, "elapsed": time.time() - t0, "msg": "Nessun filing nei settori"}
    
    # Parsing
    progress_bar = progress_ph.progress(0.0, text=f"Parsing 0/{len(relevant)}")
    
    def parse_task(filing):
        doc = edgar.fetch_form4_document(session, filing["index_url"])
        if not doc:
            return filing, None, None
        parsed = edgar.parse_form4_resilient(doc)
        if not parsed:
            return filing, None, None
        return filing, parsed, doc["url"]
    
    def cb(done, total):
        progress_bar.progress(done / total, text=f"Parsing {done}/{total}")
    
    parse_results = edgar.parallel_map(parse_task, relevant, max_workers=parse_concurrency, progress_callback=cb)
    progress_bar.empty()
    
    # ===== DEBUG STATS =====
    ds = edgar.DEBUG_STATS
    add_log("="*40)
    add_log("DEBUG STATS PARSER:")
    add_log(f"  HTTP 403 (banned): {ds['http_403']}")
    add_log(f"  HTTP 429 (rate limit): {ds['http_429']}")
    add_log(f"  HTTP altro: {ds['http_other']}")
    add_log(f"  Doc fetch failed: {ds['doc_fetch_failed']}")
    add_log(f"  No XML in index: {ds['no_xml_in_index']}")
    add_log(f"  All XML attempts failed: {ds['all_xml_attempts_failed']}")
    add_log(f"  Parse XML failed: {ds['parse_xml_failed']}")
    add_log(f"  Parse keyword failed: {ds['parse_keyword_failed']}")
    add_log(f"  Parse regex failed: {ds['parse_regex_failed']}")
    add_log(f"  Parse HTML failed: {ds['parse_html_failed']}")
    add_log(f"  ✅ Parse SUCCESS: {ds['parse_success']}")
    add_log("="*40)
    
    # Costruisci match
    new_matches = []
    cLevel_keywords = ["ceo", "cfo", "coo", "president", "chief"]
    
    for result in parse_results:
        if not result:
            continue
        filing, parsed, doc_url = result
        if not parsed or not parsed["purchases"]:
            continue
        
        if filter_clevel:
            t_low = parsed["insider_title"].lower()
            if not any(k in t_low for k in cLevel_keywords):
                continue
        
        sic = sic_map[filing["cik"]]
        sector = edgar.SIC_WHITELIST[sic]
        
        seen = set()
        for tx in parsed["purchases"]:
            if tx["total"] < soglia:
                continue
            key = (filing["accession"], tx["date"], round(tx["shares"]), round(tx["price"], 4))
            if key in seen:
                continue
            seen.add(key)
            new_matches.append({
                "accession": filing["accession"],
                "ticker": parsed["ticker"] or "N/D",
                "company": parsed["company"] or filing["company"],
                "insider_name": parsed["insider_name"],
                "insider_title": parsed["insider_title"],
                "tx_date": tx["date"],
                "filing_date": filing["date_filed"],
                "shares": tx["shares"],
                "price": tx["price"],
                "total": tx["total"],
                "sector": sector,
                "sic": sic,
                "xml_url": doc_url,
                "parser_strategy": parsed["strategy"],
            })
    
    edgar.detect_clusters(new_matches)
    saved_count = sum(1 for m in new_matches if db_save_purchase(conn, m))
    conn.commit()
    db_purge_old(conn)
    
    elapsed = time.time() - t0
    add_log(f"Salvati: {saved_count} su {len(new_matches)} match · TOTALE: {elapsed:.1f}s")
    
    return {"new_matches": saved_count, "total_match": len(new_matches), "elapsed": elapsed}


# ============================================================
# UI semplificata
# ============================================================

st.title("📊 OS Insider Scanner")
st.caption("Form 4 P+A · Biotech + Semiconductors · v3.4 DEBUG")

with st.sidebar:
    st.header("⚙️ Settings")
    soglia = st.number_input("Soglia min USD", min_value=0, value=15000, step=1000)
    filter_clevel = st.checkbox("Solo C-level", value=False)
    sort_mode = st.selectbox("Ordina per", ["Valore decrescente", "Più recenti", "Per ticker"])
    parse_concurrency = st.select_slider("Concorrenza parsing", options=[3, 5, 8], value=5)
    
    st.divider()
    if get_telegram_config():
        st.success("✅ Telegram attivo")
    else:
        st.info("ℹ️ Telegram non configurato")
    
    st.divider()
    st.subheader("📊 Settori")
    st.caption("**Biotech**: 2836, 2834, 2833, 2835, 8731")
    st.caption("**Semiconductors**: 3674, 3670, 3571, 3572, 3576, 3577")
    
    st.divider()
    if st.button("🗑 Reset accession processati", use_container_width=True):
        conn = get_db()
        conn.execute("DELETE FROM processed_filings")
        conn.commit()
        st.success("Reset fatto. Ora puoi ri-scansionare gli stessi filing.")

c1, c2 = st.columns([3, 1])
with c1:
    scan_btn = st.button("📊 Scansione 24h completa", use_container_width=True, type="primary")
with c2:
    st.write("")  # placeholder

progress_ph = st.empty()
log_expander = st.expander("📜 Log tecnico (espandi per dettagli)", expanded=True)
log_ph = log_expander.empty()

if scan_btn:
    with st.spinner("Scansione…"):
        result = run_scan_full(progress_ph, log_ph, soglia, filter_clevel, parse_concurrency)
    if "error" in result:
        progress_ph.error(f"❌ {result['error']}")
    else:
        msg = f"✅ {result['elapsed']:.1f}s — {result['new_matches']} nuovi"
        if "msg" in result:
            msg += f" · {result['msg']}"
        progress_ph.success(msg)

st.divider()

conn = get_db()
df = db_load_recent(conn, hours=24)
stats = db_stats(conn)

c1, c2, c3, c4 = st.columns(4)
with c1: st.metric("Filing 24h", len(df))
with c2:
    tv = df["total"].sum() if len(df) > 0 else 0
    val_str = f"${tv/1e6:.2f}M" if tv >= 1e6 else (f"${tv/1e3:.1f}k" if tv >= 1e3 else f"${tv:,.0f}")
    st.metric("Valore tot", val_str)
with c3:
    cc = df[df["is_cluster"] == 1]["ticker"].nunique() if len(df) > 0 else 0
    st.metric("Cluster", cc)
with c4: st.metric("Cache SIC", f"{stats['sic_cache']:,}")

if len(df) == 0:
    st.info("📭 Nessun filing in storico.")
else:
    if sort_mode == "Più recenti":
        df = df.sort_values("filing_date", ascending=False)
    elif sort_mode == "Per ticker":
        df = df.sort_values(["ticker", "total"], ascending=[True, False])
    else:
        df = df.sort_values("total", ascending=False)
    
    disp = df.copy()
    disp["💰 Tot"] = disp["total"].apply(
        lambda x: f"${x/1e6:.2f}M" if x >= 1e6 else (f"${x/1e3:.1f}k" if x >= 1e3 else f"${x:,.0f}")
    )
    disp["📊 Az."] = disp["shares"].apply(lambda x: f"{x:,.0f}")
    disp["💵 $"] = disp["price"].apply(lambda x: f"${x:.2f}")
    
    st.dataframe(
        disp[["ticker", "company", "insider_name", "insider_title", "tx_date",
              "📊 Az.", "💵 $", "💰 Tot", "sector", "parser_strategy", "xml_url"]],
        column_config={
            "ticker": "Ticker",
            "company": "Azienda",
            "insider_name": "Insider",
            "insider_title": "Ruolo",
            "tx_date": "Tx Date",
            "sector": "Settore",
            "parser_strategy": "Parser",
            "xml_url": st.column_config.LinkColumn("📄", display_text="link"),
        },
        hide_index=True,
        use_container_width=True,
    )
    
    csv = df.to_csv(index=False)
    st.download_button(
        "📥 Esporta CSV", csv,
        f"insider_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv", "text/csv",
    )

st.divider()
st.caption(f"💾 Cache SIC: {stats['sic_cache']:,} · Processati: {stats['processed']:,} · v3.4 DEBUG")
