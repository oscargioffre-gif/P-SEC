"""
fetcher.py — Script eseguito da GitHub Actions ogni 5 minuti.

Pipeline:
  1. fetch ATOM feed (ultimi 100 Form 4)
  2. prefiltro nome biotech/semi
  3. SIC lookup (con cache)
  4. parse Form 4 P+A
  5. cluster detection
  6. salva in data.json (committato dal workflow)
  7. invia Telegram alert solo per purchases NUOVI (non ancora visti)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import edgar


# ============================================================
# CONFIG
# ============================================================
DATA_FILE = Path("data.json")
RETENTION_HOURS = 28
SOGLIA_TELEGRAM = float(os.environ.get("SOGLIA_USD", "10000"))


def load_state():
    """Carica data.json o ritorna stato vuoto."""
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "purchases": [],
        "sic_cache": {},          # cik → sic
        "processed_accessions": [], # accession già processate
        "alerted_keys": [],         # chiavi univoche già alertate
        "last_update": None,
    }


def save_state(state):
    """Salva data.json. Comprimo se troppo grande."""
    # Purge old purchases
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)).isoformat()
    state["purchases"] = [p for p in state["purchases"] if p.get("detected_at", "") >= cutoff]
    
    # Limita processed_accessions a ultimi 5000 per evitare crescita infinita
    if len(state["processed_accessions"]) > 5000:
        state["processed_accessions"] = state["processed_accessions"][-5000:]
    if len(state["alerted_keys"]) > 5000:
        state["alerted_keys"] = state["alerted_keys"][-5000:]
    
    state["last_update"] = datetime.now(timezone.utc).isoformat()
    
    with open(DATA_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def send_telegram(text):
    """Invia messaggio Telegram."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return False
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        return r.ok
    except Exception:
        return False


def format_alert(p):
    """Formatta alert Telegram."""
    cluster_tag = f" ⚡<b>CLUSTER {p['cluster_size']}</b>" if p.get("is_cluster") else ""
    lines = [
        f"🔔 <b>{p['ticker']}</b>{cluster_tag} — {p['sector']}",
        f"<i>{(p.get('company') or '')[:60]}</i>",
        "",
        f"👤 {p['insider_name']} ({p.get('insider_title', 'N/D')})",
        f"💰 {p['shares']:,.0f} sh @ ${p['price']:.2f} = <b>${p['total']:,.0f}</b>",
        f"📅 Tx: {p['tx_date']}",
    ]
    if p.get("xml_url"):
        lines.append(f'🔗 <a href="{p["xml_url"]}">SEC filing</a>')
    return "\n".join(lines)


def main():
    print(f"[{datetime.now()}] Fetcher started")
    state = load_state()
    
    sic_cache = state["sic_cache"]
    processed = set(state["processed_accessions"])
    alerted = set(state["alerted_keys"])
    
    session = edgar.make_session()
    
    # 1. ATOM feed
    print("Fetching ATOM feed...")
    feed = edgar.fetch_atom_feed(session, count=100)
    print(f"  → {len(feed)} entries")
    
    if not feed:
        print("ATOM feed empty, possibly rate-limited. Saving state and exit.")
        save_state(state)
        return 0
    
    # 2. Skip già processati
    new_filings = [f for f in feed if f["accession"] not in processed]
    print(f"  → {len(new_filings)} new (not yet processed)")
    
    if not new_filings:
        print("Nothing new. Saving state and exit.")
        save_state(state)
        return 0
    
    # 3. Prefiltro nome
    candidates = [f for f in new_filings if edgar.is_candidate_company_name(f.get("company", ""))]
    print(f"  → {len(candidates)} candidates (after name prefilter)")
    
    # Marca tutti come processati a prescindere
    for f in new_filings:
        processed.add(f["accession"])
    
    if not candidates:
        state["processed_accessions"] = sorted(processed)
        save_state(state)
        return 0
    
    # 4. Parse documents (in parallelo) PRIMA del SIC lookup
    # così parsiamo solo una volta e poi usiamo issuer_cik del parsed per SIC
    print(f"Parsing {len(candidates)} candidates...")
    
    def parse_task(filing):
        doc = edgar.fetch_form4_document(session, filing["index_url"])
        if not doc:
            return None
        parsed = edgar.parse_form4_resilient(doc)
        if not parsed or not parsed["purchases"]:
            return None
        return {"filing": filing, "parsed": parsed, "doc_url": doc["url"]}
    
    parse_results = edgar.parallel_map(parse_task, candidates, max_workers=4)
    valid_results = [r for r in parse_results if r is not None]
    print(f"  → {len(valid_results)} with P+A purchases")
    
    if not valid_results:
        state["processed_accessions"] = sorted(processed)
        save_state(state)
        return 0
    
    # 5. SIC lookup solo per quelli con purchases
    ciks_needed = list(set(r["parsed"]["issuer_cik"] for r in valid_results if r["parsed"].get("issuer_cik")))
    ciks_to_fetch = [c for c in ciks_needed if c not in sic_cache]
    print(f"SIC lookup: {len(ciks_to_fetch)} new (cache hit: {len(ciks_needed) - len(ciks_to_fetch)})")
    
    for cik in ciks_to_fetch:
        sic = edgar.fetch_filer_sic(session, cik)
        sic_cache[cik] = sic or ""  # store empty string for "unknown" to avoid retry
    
    # 6. Costruisci match nei settori target
    new_purchases = []
    for r in valid_results:
        cik = r["parsed"]["issuer_cik"]
        sic = sic_cache.get(cik, "")
        if not sic or sic not in edgar.SIC_WHITELIST:
            continue
        sector = edgar.SIC_WHITELIST[sic]
        
        seen_in_filing = set()
        for tx in r["parsed"]["purchases"]:
            key_in_filing = (tx["date"], round(tx["shares"]), round(tx["price"], 4))
            if key_in_filing in seen_in_filing:
                continue
            seen_in_filing.add(key_in_filing)
            
            new_purchases.append({
                "accession": r["filing"]["accession"],
                "ticker": r["parsed"]["ticker"] or "N/D",
                "company": r["parsed"]["company"] or r["filing"].get("company", ""),
                "insider_name": r["parsed"]["insider_name"],
                "insider_title": r["parsed"]["insider_title"],
                "tx_date": tx["date"],
                "filing_date": (r["filing"].get("updated", "") or "")[:10],
                "shares": tx["shares"],
                "price": tx["price"],
                "total": tx["total"],
                "sector": sector,
                "sic": sic,
                "xml_url": r["doc_url"],
                "parser_strategy": r["parsed"]["strategy"],
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })
    
    # 7. Cluster detection (sui purchases di OGGI in storico + nuovi)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    recent_all = [p for p in state["purchases"] if p.get("tx_date") in (today, yesterday)] + new_purchases
    edgar.detect_clusters(recent_all, min_insiders=3)
    
    # Aggiorna i nuovi (cluster info copiato indietro)
    new_keys = set((p["accession"], p["insider_name"], p["tx_date"], p["shares"]) for p in new_purchases)
    for p in recent_all:
        k = (p["accession"], p["insider_name"], p["tx_date"], p["shares"])
        if k in new_keys:
            for np in new_purchases:
                if (np["accession"], np["insider_name"], np["tx_date"], np["shares"]) == k:
                    np["is_cluster"] = p["is_cluster"]
                    np["cluster_size"] = p["cluster_size"]
                    break
    
    # 8. Append nuovi al state (dedup su key univoca)
    existing_keys = set(
        (p["accession"], p["insider_name"], p.get("tx_date", ""), round(p.get("shares", 0)), round(p.get("price", 0), 4))
        for p in state["purchases"]
    )
    for np in new_purchases:
        k = (np["accession"], np["insider_name"], np["tx_date"], round(np["shares"]), round(np["price"], 4))
        if k not in existing_keys:
            state["purchases"].append(np)
            existing_keys.add(k)
    
    print(f"  → {len(new_purchases)} new purchases in target sectors")
    
    # 9. Telegram alerts (solo nuovi sopra soglia)
    alerts_sent = 0
    for np in new_purchases:
        if np["total"] < SOGLIA_TELEGRAM:
            continue
        alert_key = f"{np['accession']}|{np['insider_name']}|{np['tx_date']}|{int(np['shares'])}|{np['price']:.4f}"
        if alert_key in alerted:
            continue
        if send_telegram(format_alert(np)):
            alerted.add(alert_key)
            alerts_sent += 1
            time.sleep(0.5)  # rate limit Telegram
    
    print(f"  → {alerts_sent} Telegram alerts sent")
    
    # 10. Salva stato
    state["sic_cache"] = sic_cache
    state["processed_accessions"] = sorted(processed)
    state["alerted_keys"] = sorted(alerted)
    save_state(state)
    
    print(f"[{datetime.now()}] Fetcher complete. Total purchases in storage: {len(state['purchases'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
