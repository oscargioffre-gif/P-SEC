"""
fetcher.py v4 — Self-healing recovery

Detect+recover quando GitHub Actions cron salta un run.
- Confronta last_update con cron atteso (varia per orario)
- Se gap troppo grande, attiva RECOVERY MODE: scarica 200 entries invece di 100
- Logga il recovery in last_run_stats per visibilità nell'app
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import edgar


DATA_FILE = Path("data.json")
RETENTION_HOURS = 28
SOGLIA_TELEGRAM = float(os.environ.get("SOGLIA_USD", "10000"))


# ============================================================
# CRON SCHEDULE INFERENCE
# ============================================================

def get_expected_cron_minutes(now_utc):
    """Ritorna i minuti attesi tra cron in base all'orario corrente."""
    weekday = now_utc.weekday()  # 0=Mon, 6=Sun
    hour = now_utc.hour
    
    # Weekend: cron ogni ora
    if weekday in (5, 6):  # Sat, Sun
        return 60
    
    # Weekday US market hours (13-19 UTC = 15:30-21:00 IT con qualche margine)
    if 13 <= hour <= 19:
        return 2
    
    # Weekday post-close (20-21 UTC = 22:00-00:00 IT)
    if hour in (20, 21):
        return 5
    
    # Weekday night/morning (resto)
    return 30


def get_cron_label(expected_minutes):
    """Etichetta human-readable per il cron corrente."""
    if expected_minutes == 2:
        return "mercato US (2 min)"
    elif expected_minutes == 5:
        return "post-close (5 min)"
    elif expected_minutes == 30:
        return "notte/mattina (30 min)"
    elif expected_minutes == 60:
        return "weekend (1h)"
    else:
        return f"{expected_minutes} min"


# ============================================================
# STATE I/O
# ============================================================

def load_state():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "purchases": [],
        "sic_cache": {},
        "processed_accessions": [],
        "alerted_keys": [],
        "last_update": None,
        "run_count": 0,
        "last_run_stats": {},
    }


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)).isoformat()
    state["purchases"] = [p for p in state["purchases"] if p.get("detected_at", "") >= cutoff]
    
    if len(state["processed_accessions"]) > 5000:
        state["processed_accessions"] = state["processed_accessions"][-5000:]
    if len(state["alerted_keys"]) > 5000:
        state["alerted_keys"] = state["alerted_keys"][-5000:]
    
    state["last_update"] = datetime.now(timezone.utc).isoformat()
    
    with open(DATA_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):
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


def format_alert(p, recovery=False):
    cluster_tag = f" ⚡<b>CLUSTER {p['cluster_size']}</b>" if p.get("is_cluster") else ""
    recovery_tag = " 🔧<i>recovered</i>" if recovery else ""
    
    filing_us_str = ""
    if p.get("filing_datetime_utc"):
        try:
            dt_utc = datetime.fromisoformat(p["filing_datetime_utc"].replace("Z", "+00:00"))
            dt_et = dt_utc - timedelta(hours=4)
            filing_us_str = f"📅 Filed: {dt_et.strftime('%d %b %H:%M')} ET"
        except (ValueError, AttributeError):
            pass
    
    lines = [
        f"🔔 <b>{p['ticker']}</b>{cluster_tag}{recovery_tag} — {p['sector']}",
        f"<i>{(p.get('company') or '')[:60]}</i>",
        "",
        f"👤 {p['insider_name']} ({p.get('insider_title', 'N/D')})",
        f"💰 {p['shares']:,.0f} sh @ ${p['price']:.2f} = <b>${p['total']:,.0f}</b>",
        f"📆 Tx: {p['tx_date']}",
    ]
    if filing_us_str:
        lines.append(filing_us_str)
    if p.get("xml_url"):
        lines.append(f'🔗 <a href="{p["xml_url"]}">SEC filing</a>')
    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main():
    now_utc = datetime.now(timezone.utc)
    print(f"[{now_utc}] Fetcher v4 started")
    
    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1
    
    # ============================================================
    # SELF-HEALING DETECTION
    # ============================================================
    
    expected_cron_min = get_expected_cron_minutes(now_utc)
    cron_label = get_cron_label(expected_cron_min)
    
    recovery_mode = False
    skipped_runs = 0
    last_update_str = state.get("last_update")
    
    if last_update_str:
        try:
            last_update_dt = datetime.fromisoformat(last_update_str.replace("Z", "+00:00"))
            if last_update_dt.tzinfo is None:
                last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)
            
            gap_minutes = (now_utc - last_update_dt).total_seconds() / 60
            
            # Soglia recovery: 2.5x il cron atteso (margin per skip occasionali)
            recovery_threshold = expected_cron_min * 2.5
            
            if gap_minutes > recovery_threshold:
                recovery_mode = True
                skipped_runs = max(1, int(gap_minutes / expected_cron_min) - 1)
                print(f"⚠️  RECOVERY MODE: gap={gap_minutes:.1f}min > threshold={recovery_threshold:.1f}min")
                print(f"   Estimated skipped runs: {skipped_runs}")
                print(f"   Cron label: {cron_label}")
            else:
                print(f"OK: gap={gap_minutes:.1f}min, threshold={recovery_threshold:.1f}min ({cron_label})")
        except (ValueError, AttributeError) as e:
            print(f"Could not parse last_update: {e}")
    else:
        print("First run, no last_update reference")
    
    # ============================================================
    # SCAN
    # ============================================================
    
    sic_cache = state["sic_cache"]
    processed = set(state["processed_accessions"])
    alerted = set(state["alerted_keys"])
    
    session = edgar.make_session()
    
    # Adatta count: 200 in recovery, 100 normale
    feed_count_param = 200 if recovery_mode else 100
    print(f"Fetching ATOM feed (count={feed_count_param})...")
    feed = edgar.fetch_atom_feed(session, count=feed_count_param)
    feed_count = len(feed)
    print(f"  -> {feed_count} entries")
    
    new_filings_count = 0
    candidates_count = 0
    valid_count = 0
    new_purchases_count = 0
    alerts_sent = 0
    
    if feed:
        new_filings = [f for f in feed if f["accession"] not in processed]
        new_filings_count = len(new_filings)
        print(f"  -> {new_filings_count} new (not yet processed)")
        
        if new_filings:
            candidates = [f for f in new_filings if edgar.is_candidate_company_name(f.get("company", ""))]
            candidates_count = len(candidates)
            print(f"  -> {candidates_count} candidates (after name prefilter)")
            
            for f in new_filings:
                processed.add(f["accession"])
            
            if candidates:
                print(f"Parsing {candidates_count} candidates...")
                
                def parse_task(filing):
                    doc = edgar.fetch_form4_document(session, filing["index_url"])
                    if not doc:
                        return None
                    parsed = edgar.parse_form4_resilient(doc)
                    if not parsed or not parsed["purchases"]:
                        return None
                    return {"filing": filing, "parsed": parsed, "doc_url": doc["url"]}
                
                # In recovery mode usa più workers (più velocità su backlog)
                max_workers = 6 if recovery_mode else 4
                parse_results = edgar.parallel_map(parse_task, candidates, max_workers=max_workers)
                valid_results = [r for r in parse_results if r is not None]
                valid_count = len(valid_results)
                print(f"  -> {valid_count} with P+A purchases")
                
                if valid_results:
                    ciks_needed = list(set(r["parsed"]["issuer_cik"] for r in valid_results if r["parsed"].get("issuer_cik")))
                    ciks_to_fetch = [c for c in ciks_needed if c not in sic_cache]
                    print(f"SIC lookup: {len(ciks_to_fetch)} new (cache hit: {len(ciks_needed) - len(ciks_to_fetch)})")
                    
                    for cik in ciks_to_fetch:
                        sic = edgar.fetch_filer_sic(session, cik)
                        sic_cache[cik] = sic or ""
                    
                    new_purchases = []
                    for r in valid_results:
                        cik = r["parsed"]["issuer_cik"]
                        sic = sic_cache.get(cik, "")
                        if not sic or sic not in edgar.SIC_WHITELIST:
                            continue
                        sector = edgar.SIC_WHITELIST[sic]
                        
                        filing_dt_utc = r["filing"].get("updated", "") or ""
                        filing_date_short = filing_dt_utc[:10] if filing_dt_utc else ""
                        
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
                                "filing_date": filing_date_short,
                                "filing_datetime_utc": filing_dt_utc,
                                "shares": tx["shares"],
                                "price": tx["price"],
                                "total": tx["total"],
                                "sector": sector,
                                "sic": sic,
                                "xml_url": r["doc_url"],
                                "parser_strategy": r["parsed"]["strategy"],
                                "detected_at": now_utc.isoformat(),
                                "from_recovery": recovery_mode,  # NUOVO: flag recovery
                            })
                    
                    new_purchases_count = len(new_purchases)
                    
                    today = now_utc.strftime("%Y-%m-%d")
                    yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
                    recent_all = [p for p in state["purchases"] if p.get("tx_date") in (today, yesterday)] + new_purchases
                    edgar.detect_clusters(recent_all, min_insiders=3)
                    
                    new_keys = set((p["accession"], p["insider_name"], p["tx_date"], p["shares"]) for p in new_purchases)
                    for p in recent_all:
                        k = (p["accession"], p["insider_name"], p["tx_date"], p["shares"])
                        if k in new_keys:
                            for np in new_purchases:
                                if (np["accession"], np["insider_name"], np["tx_date"], np["shares"]) == k:
                                    np["is_cluster"] = p["is_cluster"]
                                    np["cluster_size"] = p["cluster_size"]
                                    break
                    
                    existing_keys = set(
                        (p["accession"], p["insider_name"], p.get("tx_date", ""), round(p.get("shares", 0)), round(p.get("price", 0), 4))
                        for p in state["purchases"]
                    )
                    for np in new_purchases:
                        k = (np["accession"], np["insider_name"], np["tx_date"], round(np["shares"]), round(np["price"], 4))
                        if k not in existing_keys:
                            state["purchases"].append(np)
                            existing_keys.add(k)
                    
                    print(f"  -> {new_purchases_count} new purchases in target sectors")
                    
                    for np in new_purchases:
                        if np["total"] < SOGLIA_TELEGRAM:
                            continue
                        alert_key = f"{np['accession']}|{np['insider_name']}|{np['tx_date']}|{int(np['shares'])}|{np['price']:.4f}"
                        if alert_key in alerted:
                            continue
                        if send_telegram(format_alert(np, recovery=recovery_mode)):
                            alerted.add(alert_key)
                            alerts_sent += 1
                            time.sleep(0.5)
                    
                    print(f"  -> {alerts_sent} Telegram alerts sent")
    
    # ============================================================
    # HEARTBEAT STATS (sempre aggiornato)
    # ============================================================
    
    state["last_run_stats"] = {
        "timestamp": now_utc.isoformat(),
        "feed_count": feed_count,
        "new_filings": new_filings_count,
        "candidates": candidates_count,
        "valid_with_purchases": valid_count,
        "new_purchases_in_target": new_purchases_count,
        "telegram_alerts_sent": alerts_sent,
        "recovery_mode": recovery_mode,
        "skipped_runs_estimate": skipped_runs,
        "cron_label": cron_label,
        "expected_cron_minutes": expected_cron_min,
        "feed_count_param": feed_count_param,
    }
    
    state["sic_cache"] = sic_cache
    state["processed_accessions"] = sorted(processed)
    state["alerted_keys"] = sorted(alerted)
    save_state(state)
    
    mode_str = "🔧 RECOVERY" if recovery_mode else "✅ normal"
    print(f"[{datetime.now()}] Fetcher complete. Run #{state['run_count']} ({mode_str}). Storage: {len(state['purchases'])} purchases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
