"""
edgar.py — v3.4 con debug logging dettagliato.
Modifiche rispetto v3.3:
- Header Host RIMOSSO (era ridondante e potenzialmente problematico)
- Logging strutturato di ogni step del parsing
- Statistiche failure dettagliate (non solo "fail=67")
"""

import re
import threading
import time
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)

# Stat globali per debug — accessibili da app.py
DEBUG_STATS = {
    "doc_fetch_failed": 0,         # fetch_form4_document ha ritornato None
    "no_xml_in_index": 0,          # nessun .xml o .htm trovato nell'index
    "all_xml_attempts_failed": 0,  # tutti gli XML provati erano vuoti/non Form 4
    "xml_404": 0,                  # XML restituì 404 o errore
    "parse_xml_failed": 0,         # parser XML ha ritornato None
    "parse_keyword_failed": 0,     # parser keyword ha ritornato None
    "parse_regex_failed": 0,       # parser regex ha ritornato None
    "parse_html_failed": 0,        # parser HTML ha ritornato None
    "parse_success": 0,
    "http_403": 0,
    "http_429": 0,
    "http_other": 0,
}

def reset_debug_stats():
    for k in DEBUG_STATS:
        DEBUG_STATS[k] = 0


# ============================================================
# SETTORI
# ============================================================
SIC_CODES = {
    "Biotech":        {"2836", "2834", "2833", "2835", "8731"},
    "Semiconductors": {"3674", "3670", "3571", "3572", "3576", "3577"},
}
SIC_WHITELIST: dict[str, str] = {}
for sector, sics in SIC_CODES.items():
    for sic in sics:
        SIC_WHITELIST[sic] = sector


# ============================================================
# PREFILTRO NOME — keyword espanse
# ============================================================
COMPANY_NAME_KEYWORDS = [
    # Biotech core
    "BIO", "BIOTECH", "BIOTECHNOLOGY", "BIOSCIENCE", "BIOSCIENCES",
    "BIOLOGICS", "BIOLOGICAL", "BIOPHARMA", "BIOPHARMACEUT",
    "BIOMED", "BIOMARKER",
    # Pharma
    "PHARMA", "PHARMACEUT", "PHARMACEUTICAL", "PHARMACOL", "PHARM",
    # Therapeutics
    "THERAPEUT", "THERAPY", "THERAPIES", "THERA",
    # Sciences/Research/Labs
    "SCIENCE", "SCIENCES", "SCIENTIFIC", "RESEARCH",
    "LABS", "LABORATOR", "LIFE SCI", "LIFESCI",
    "MEDICINES", "MEDICINE", "MEDS",
    # Genetics/Molecular
    "GENOMIC", "GENOMICS", "GENETIC", "GENETICS", "GENE",
    "MOLECULAR", "MOLECULE", "PROTEIN", "PROTEINS",
    "PEPTIDE", "PEPTIDES", "RNA", "DNA", "ANTIBOD",
    "EPIGEN", "TRANSCRIPT", "OLIGO",
    # Cells/Tissue/Immune
    "CELL", "CELLS", "CELLULAR", "TISSUE", "STEM",
    "IMMUN", "IMMUNE", "IMMUNOLOG", "IMMUNOTH", "IMMUNOTHERAPY",
    "ANTIGEN", "T-CELL", "CAR-T",
    # Disease
    "ONCOLOG", "ONCOLOGY", "CANCER", "TUMOR", "TUMORS",
    "NEURO", "NEUROLOG", "NEUROSCIENCE",
    "CARDIO", "CARDIAC", "CARDIOVASC",
    "METABOL", "INFLAMM", "FIBROSIS",
    "DIABETES", "OBESITY", "OPHTHALM", "DERMAT",
    "ALZHEIMER", "PARKINSON",
    # Treatment
    "VACCIN", "ANTIVIRAL", "ANTIBIOTIC", "ANTIBIOTICS",
    "DRUG", "DRUGS", "CLINICAL", "TRIAL",
    # Diagnostic
    "DIAGNOSTIC", "DIAGNOSTICS", "IMAGING",
    # Health
    "HEALTH", "HEALTHCARE",
    # Semiconductors
    "SEMI", "SEMICONDUCTOR", "SEMICONDUCTORS",
    "CHIP", "CHIPS",
    "MICRO", "MICROELECT", "MICROELECTRON",
    "SILICON", "WAFER",
    "FOUNDRY", "FAB ", "FABRICATION",
    "LITHOGRAPHY", "ETCH", "DEPOSITION",
    "EQUIPMENT", "TOOLING",
    "INTEGRATED CIRCUIT", "MEMORY", "DRAM", "FLASH",
    "PROCESSOR", "CPU", "GPU", "FPGA", "ASIC", "MCU",
    "SENSOR", "SENSORS",
    "PHOTONIC", "PHOTONICS", "OPTOELECTRONIC",
    "LASER", "LIDAR", "LED",
    "ELECTRONIC", "ELECTRONICS",
    "NANO", "NANOTECH", "MICROTECH",
]


def is_candidate_company_name(name: str) -> bool:
    if not name:
        return False
    name_upper = name.upper()
    return any(kw in name_upper for kw in COMPANY_NAME_KEYWORDS)


# ============================================================
# CONFIG HTTP
# ============================================================
USER_AGENT_TEMPLATE = "OS Insider Scanner ({email})"
SEC_BASE = "https://www.sec.gov"
SEC_DATA_BASE = "https://data.sec.gov"

HOST_RATE_LIMITS = {
    "www.sec.gov": 5.0,
    "data.sec.gov": 2.0,
}
DEFAULT_RATE = 3.0

SCHEMA = {
    "shares_min": 0.01, "shares_max": 1e9,
    "price_min": 0.0001, "price_max": 1e6,
    "total_min": 1, "total_max": 1e10,
    "max_age_days": 365 * 5,
}


# ============================================================
# RATE LIMITER
# ============================================================
class HostRateLimiter:
    def __init__(self):
        self._buckets: dict[str, deque] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
    
    def _get_lock(self, host: str) -> threading.Lock:
        with self._global_lock:
            if host not in self._locks:
                self._locks[host] = threading.Lock()
            return self._locks[host]
    
    def wait(self, host: str):
        rate = HOST_RATE_LIMITS.get(host, DEFAULT_RATE)
        min_interval = 1.0 / rate
        
        lock = self._get_lock(host)
        with lock:
            if host not in self._buckets:
                self._buckets[host] = deque(maxlen=int(rate) + 2)
            
            bucket = self._buckets[host]
            now = time.monotonic()
            
            while bucket and bucket[0] < now - 1.0:
                bucket.popleft()
            
            if len(bucket) >= int(rate):
                wait_until = bucket[0] + 1.0
                sleep_time = wait_until - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                bucket.popleft()
            
            if bucket:
                last = bucket[-1]
                gap = time.monotonic() - last
                if gap < min_interval:
                    time.sleep(min_interval - gap)
            
            bucket.append(time.monotonic())


_rate_limiter = HostRateLimiter()


# ============================================================
# HTTP SESSION
# ============================================================

def make_session(email: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT_TEMPLATE.format(email=email),
        "Accept-Encoding": "gzip, deflate",
        "Accept": "*/*",
        # NB: NO header Host — viene impostato automaticamente da requests
    })
    return s


def safe_get(session: requests.Session, url: str, timeout: int = 15, retries: int = 3) -> Optional[requests.Response]:
    """GET con rate limiting + retry intelligente. NO header Host (causava problemi)."""
    host = url.split("//")[1].split("/")[0]
    
    for attempt in range(retries + 1):
        _rate_limiter.wait(host)
        try:
            r = session.get(url, timeout=timeout)
            
            if r.status_code == 200:
                return r
            
            if r.status_code == 429:
                DEBUG_STATS["http_429"] += 1
                wait = min(60, 5 * (2 ** attempt))
                logger.warning(f"429 attempt {attempt+1}: wait {wait}s — {url[-60:]}")
                time.sleep(wait)
                continue
            
            if r.status_code == 403:
                DEBUG_STATS["http_403"] += 1
                logger.warning(f"403 Forbidden: {url[-60:]}")
                return None
            
            if r.status_code == 404:
                return None
            
            DEBUG_STATS["http_other"] += 1
            logger.warning(f"HTTP {r.status_code}: {url[-60:]}")
            
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                time.sleep(2 + attempt)
                continue
            logger.warning(f"Request fail: {url[-60:]} — {type(e).__name__}")
    
    return None


# ============================================================
# DATE
# ============================================================

def get_target_dates(num_days: int = 2) -> list[dict]:
    out = []
    d = datetime.now(timezone.utc)
    offset = 0
    while len(out) < num_days and offset < 7:
        candidate = d - timedelta(days=offset)
        if candidate.weekday() < 5:
            out.append({
                "year": candidate.year,
                "qtr": (candidate.month - 1) // 3 + 1,
                "yyyymmdd": candidate.strftime("%Y%m%d"),
            })
        offset += 1
    return out


# ============================================================
# FETCH FILE LISTS
# ============================================================

def fetch_full_index(session: requests.Session, target_date: dict) -> list[dict]:
    url = (f"{SEC_BASE}/Archives/edgar/daily-index/"
           f"{target_date['year']}/QTR{target_date['qtr']}/form.{target_date['yyyymmdd']}.idx")
    r = safe_get(session, url, timeout=30)
    if not r:
        return []
    
    out = []
    for line in r.text.splitlines():
        if not re.match(r"^4\s+[A-Za-z0-9]", line):
            continue
        form_type = line[0:12].strip()
        if form_type != "4":
            continue
        company = line[12:74].strip()
        cik = line[74:86].strip()
        date_filed = line[86:98].strip()
        filename = line[98:].strip()
        if not filename:
            continue
        m = re.search(r"(\d{10}-\d{2}-\d{6})", filename)
        if not m:
            continue
        out.append({
            "accession": m.group(1),
            "company": company,
            "cik": cik,
            "date_filed": date_filed,
            "index_url": f"{SEC_BASE}/" + filename.lstrip("/"),
        })
    return out


def fetch_atom_feed(session: requests.Session, count: int = 100) -> list[dict]:
    url = (f"{SEC_BASE}/cgi-bin/browse-edgar?action=getcurrent&type=4"
           f"&company=&dateb=&owner=include&count={count}&output=atom")
    r = safe_get(session, url)
    if not r:
        return []
    
    out = []
    try:
        text = re.sub(r'\sxmlns="[^"]+"', '', r.text, count=1)
        root = ET.fromstring(text)
        for entry in root.findall("entry"):
            link_el = entry.find("link")
            updated_el = entry.find("updated")
            title_el = entry.find("title")
            href = link_el.get("href") if link_el is not None else ""
            updated = updated_el.text if updated_el is not None else ""
            title = title_el.text if title_el is not None else ""
            
            accession = None
            if "/Archives/edgar/data/" in href:
                parts = href.rstrip("/").split("/")
                accession = parts[-1].replace("-index.htm", "").replace("-index.html", "")
            
            if accession:
                out.append({
                    "accession": accession,
                    "index_url": href,
                    "updated": updated,
                    "company": title or "",
                })
    except ET.ParseError as e:
        logger.error(f"ATOM parse error: {e}")
    
    return out


def fetch_filer_sic(session: requests.Session, cik: str) -> Optional[str]:
    cik_padded = str(cik).zfill(10)
    url = f"{SEC_DATA_BASE}/submissions/CIK{cik_padded}.json"
    r = safe_get(session, url, timeout=10)
    if not r:
        return None
    try:
        data = r.json()
        sic = str(data.get("sic", "")).strip()
        return sic if sic else None
    except (ValueError, KeyError):
        return None


# ============================================================
# FORM 4 DOCUMENT FETCH — con debug
# ============================================================

def fetch_form4_document(session: requests.Session, index_url: str) -> Optional[dict]:
    r = safe_get(session, index_url)
    if not r:
        DEBUG_STATS["doc_fetch_failed"] += 1
        return None
    
    html = r.text
    xml_urls = []
    html_urls = []
    
    for match in re.finditer(r'href="([^"]+)"', html, re.IGNORECASE):
        path = match.group(1)
        low = path.lower()
        if "/archives/" not in low:
            continue
        full = f"{SEC_BASE}{path}" if path.startswith("/") else path
        if low.endswith(".xml"):
            xml_urls.append(full)
        elif (low.endswith(".htm") or low.endswith(".html")):
            if not (low.endswith("-index.htm") or low.endswith("-index.html")):
                html_urls.append(full)
    
    if not xml_urls and not html_urls:
        DEBUG_STATS["no_xml_in_index"] += 1
        logger.warning(f"No XML/HTML in index: {index_url[-60:]}")
        return None
    
    def xml_priority(url):
        low = url.lower()
        score = 0
        if "primary" in low: score -= 10
        if "form4" in low: score -= 5
        if "/xsl" in low: score += 10
        if "ownership" in low: score -= 3
        return score
    
    xml_urls.sort(key=xml_priority)
    
    # Tenta XML
    for url in xml_urls[:5]:
        rx = safe_get(session, url)
        if not rx:
            continue
        text = rx.text
        if "<issuer" in text or ":issuer" in text or "ownershipDocument" in text:
            return {"type": "xml", "text": text, "url": url}
    
    # Tenta HTML fallback
    for url in html_urls[:3]:
        rx = safe_get(session, url)
        if not rx:
            continue
        text = rx.text
        low = text.lower()
        if "table i" in low or "non-derivative" in low or "transaction code" in low:
            return {"type": "html", "text": text, "url": url}
    
    DEBUG_STATS["all_xml_attempts_failed"] += 1
    logger.warning(f"All XML/HTML attempts failed: {index_url[-60:]} (xml={len(xml_urls)}, html={len(html_urls)})")
    return None


# ============================================================
# PARSER STRATEGIES
# ============================================================

def _strip_namespace(root: ET.Element) -> None:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def parse_xml_standard(xml_text: str) -> Optional[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    
    _strip_namespace(root)
    issuer = root.find("issuer")
    if issuer is None:
        return None
    
    def txt(parent, tag):
        el = parent.find(tag) if parent is not None else None
        return (el.text or "").strip() if el is not None and el.text else ""
    
    def nested_txt(parent, path):
        cur = parent
        for p in path.split("/"):
            if cur is None: return ""
            cur = cur.find(p)
        return (cur.text or "").strip() if cur is not None and cur.text else ""
    
    result = {
        "issuer_cik": txt(issuer, "issuerCik"),
        "company": txt(issuer, "issuerName"),
        "ticker": txt(issuer, "issuerTradingSymbol"),
        "insider_name": "",
        "insider_title": "N/D",
        "purchases": [],
        "strategy": "xml",
    }
    
    owner = root.find("reportingOwner")
    if owner is not None:
        result["insider_name"] = nested_txt(owner, "reportingOwnerId/rptOwnerName")
        rel = owner.find("reportingOwnerRelationship")
        if rel is not None:
            bits = []
            if (txt(rel, "isDirector") or "").lower() in ("1", "true"):
                bits.append("Director")
            if (txt(rel, "isOfficer") or "").lower() in ("1", "true"):
                bits.append(txt(rel, "officerTitle") or "Officer")
            if (txt(rel, "isTenPercentOwner") or "").lower() in ("1", "true"):
                bits.append("10% Owner")
            if bits:
                result["insider_title"] = ", ".join(bits)
    
    for tx in root.iter("nonDerivativeTransaction"):
        coding = tx.find("transactionCoding")
        amounts = tx.find("transactionAmounts")
        if coding is None or amounts is None:
            continue
        if txt(coding, "transactionCode") != "P":
            continue
        if nested_txt(amounts, "transactionAcquiredDisposedCode/value") != "A":
            continue
        date = nested_txt(tx, "transactionDate/value")
        try:
            shares = float(nested_txt(amounts, "transactionShares/value") or "0")
            price = float(nested_txt(amounts, "transactionPricePerShare/value") or "0")
        except ValueError:
            continue
        if shares == 0:
            continue
        result["purchases"].append({"date": date, "shares": shares, "price": price, "total": shares * price})
    
    return result


def parse_xml_keyword(xml_text: str) -> Optional[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    
    _strip_namespace(root)
    all_elements = list(root.iter())
    
    def find_by_fuzzy(kw):
        for el in all_elements:
            if kw.lower() in el.tag.lower():
                return el
        return None
    
    def find_child_fuzzy(parent, kw):
        if parent is None:
            return None
        for el in parent.iter():
            if el is parent: continue
            if kw.lower() in el.tag.lower():
                return el
        return None
    
    def text_of(el):
        return (el.text or "").strip() if el is not None and el.text else ""
    
    issuer_el = find_by_fuzzy("issuer")
    if issuer_el is None:
        return None
    
    result = {
        "issuer_cik": text_of(find_child_fuzzy(issuer_el, "cik")),
        "company": text_of(find_child_fuzzy(issuer_el, "name")),
        "ticker": text_of(find_child_fuzzy(issuer_el, "trading")),
        "insider_name": "", "insider_title": "N/D",
        "purchases": [], "strategy": "keyword",
    }
    
    owner_el = find_by_fuzzy("reportingowner")
    if owner_el is not None:
        owner_name = find_child_fuzzy(owner_el, "ownername") or find_child_fuzzy(owner_el, "rptownername")
        result["insider_name"] = text_of(owner_name)
    
    tx_blocks = [el for el in all_elements if "nonderivativetransaction" in el.tag.lower()]
    for block in tx_blocks:
        code_el = find_child_fuzzy(block, "transactioncode")
        if not code_el or text_of(code_el) != "P":
            continue
        ad_container = find_child_fuzzy(block, "acquireddisposed")
        ad_el = find_child_fuzzy(ad_container, "value") if ad_container is not None else None
        if not ad_el or text_of(ad_el) != "A":
            continue
        date_container = find_child_fuzzy(block, "transactiondate")
        date_el = find_child_fuzzy(date_container, "value") if date_container is not None else None
        shares_container = find_child_fuzzy(block, "transactionshares")
        shares_el = find_child_fuzzy(shares_container, "value") if shares_container is not None else None
        price_container = find_child_fuzzy(block, "transactionpriceper")
        price_el = find_child_fuzzy(price_container, "value") if price_container is not None else None
        try:
            shares = float(text_of(shares_el) or "0")
            price = float(text_of(price_el) or "0")
        except ValueError:
            continue
        if shares == 0:
            continue
        result["purchases"].append({"date": text_of(date_el), "shares": shares, "price": price, "total": shares * price})
    
    if not tx_blocks and not result["purchases"]:
        return None
    return result


def parse_xml_regex(xml_text: str) -> Optional[dict]:
    cik_m = re.search(r"<[^>]*issuerCik[^>]*>(\d+)<", xml_text, re.IGNORECASE)
    company_m = re.search(r"<[^>]*issuerName[^>]*>([^<]+)<", xml_text, re.IGNORECASE)
    ticker_m = re.search(r"<[^>]*issuerTradingSymbol[^>]*>([^<]+)<", xml_text, re.IGNORECASE)
    
    if not cik_m and not company_m:
        return None
    
    owner_m = re.search(r"<[^>]*rptOwnerName[^>]*>([^<]+)<", xml_text, re.IGNORECASE)
    
    result = {
        "issuer_cik": cik_m.group(1) if cik_m else "",
        "company": company_m.group(1).strip() if company_m else "",
        "ticker": ticker_m.group(1).strip() if ticker_m else "",
        "insider_name": owner_m.group(1).strip() if owner_m else "",
        "insider_title": "N/D (regex)",
        "purchases": [], "strategy": "regex",
    }
    
    block_re = re.compile(
        r"<[^>]*nonDerivativeTransaction[^>]*>(.*?)</[^>]*nonDerivativeTransaction>",
        re.IGNORECASE | re.DOTALL
    )
    
    for m in block_re.finditer(xml_text):
        block = m.group(1)
        code_m = re.search(r"<[^>]*transactionCode[^>]*>([^<]+)<", block, re.IGNORECASE)
        if not code_m or code_m.group(1).strip() != "P":
            continue
        ad_m = re.search(r"<[^>]*transactionAcquiredDisposedCode[^>]*>.*?<[^>]*value[^>]*>([^<]+)<", block, re.IGNORECASE | re.DOTALL)
        if not ad_m or ad_m.group(1).strip() != "A":
            continue
        date_m = re.search(r"<[^>]*transactionDate[^>]*>.*?<[^>]*value[^>]*>([^<]+)<", block, re.IGNORECASE | re.DOTALL)
        shares_m = re.search(r"<[^>]*transactionShares[^>]*>.*?<[^>]*value[^>]*>([^<]+)<", block, re.IGNORECASE | re.DOTALL)
        price_m = re.search(r"<[^>]*transactionPricePerShare[^>]*>.*?<[^>]*value[^>]*>([^<]+)<", block, re.IGNORECASE | re.DOTALL)
        try:
            shares = float(shares_m.group(1)) if shares_m else 0
            price = float(price_m.group(1)) if price_m else 0
        except (ValueError, AttributeError):
            continue
        if shares == 0:
            continue
        result["purchases"].append({
            "date": date_m.group(1).strip() if date_m else "",
            "shares": shares, "price": price, "total": shares * price,
        })
    
    return result


def parse_html_table(html_text: str) -> Optional[dict]:
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html_text, re.IGNORECASE | re.DOTALL)
    target_table = None
    for t in tables:
        low = t.lower()
        if "transaction code" in low and ("acquired" in low or "disposed" in low):
            target_table = t
            break
    if not target_table:
        return None
    
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", target_table, re.IGNORECASE | re.DOTALL)
    if not rows:
        return None
    
    def cells_of(row):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.IGNORECASE | re.DOTALL)
        return [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
    
    header_cells = cells_of(rows[0])
    col_idx = {}
    for i, h in enumerate(header_cells):
        h_low = h.lower()
        if "transaction code" in h_low or h_low == "code":
            col_idx["code"] = i
        elif "acquired" in h_low or "disposed" in h_low:
            col_idx["ad"] = i
        elif any(k in h_low for k in ["amount", "shares", "number"]):
            col_idx["shares"] = i
        elif "price" in h_low:
            col_idx["price"] = i
        elif "date" in h_low and "execution" not in h_low:
            col_idx["date"] = i
    
    if "code" not in col_idx:
        return None
    
    cik_m = re.search(r"CIK[^\d]*(\d{4,})", html_text, re.IGNORECASE)
    result = {
        "issuer_cik": cik_m.group(1) if cik_m else "",
        "company": "", "ticker": "", "insider_name": "",
        "insider_title": "N/D (html)",
        "purchases": [], "strategy": "html",
    }
    
    for row in rows[1:]:
        cells = cells_of(row)
        if len(cells) < 3:
            continue
        if col_idx["code"] >= len(cells):
            continue
        if cells[col_idx["code"]].strip() != "P":
            continue
        ad = cells[col_idx["ad"]].strip() if "ad" in col_idx and col_idx["ad"] < len(cells) else ""
        if ad and ad != "A":
            continue
        try:
            shares_str = cells[col_idx["shares"]].replace(",", "").replace("$", "").strip() if "shares" in col_idx else "0"
            price_str = cells[col_idx["price"]].replace(",", "").replace("$", "").strip() if "price" in col_idx else "0"
            shares = float(shares_str)
            price = float(price_str)
        except (ValueError, IndexError):
            continue
        if shares == 0:
            continue
        date = cells[col_idx["date"]] if "date" in col_idx and col_idx["date"] < len(cells) else ""
        result["purchases"].append({"date": date, "shares": shares, "price": price, "total": shares * price})
    
    return result


def validate_purchases(purchases: list[dict]) -> list[dict]:
    valid = []
    now = datetime.now(timezone.utc)
    for p in purchases:
        if not (SCHEMA["shares_min"] <= p["shares"] <= SCHEMA["shares_max"]):
            continue
        if not (SCHEMA["price_min"] <= p["price"] <= SCHEMA["price_max"]):
            continue
        if not (SCHEMA["total_min"] <= p["total"] <= SCHEMA["total_max"]):
            continue
        if p["date"]:
            try:
                d = datetime.strptime(p["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age = (now - d).days
                if age > SCHEMA["max_age_days"] or age < -7:
                    continue
            except ValueError:
                continue
        valid.append(p)
    return valid


def parse_form4_resilient(doc: dict) -> Optional[dict]:
    if doc["type"] == "xml":
        # Strategy 1: standard
        try:
            r = parse_xml_standard(doc["text"])
            if r is not None:
                r["purchases"] = validate_purchases(r["purchases"])
                DEBUG_STATS["parse_success"] += 1
                return r
        except Exception as e:
            logger.warning(f"parse_xml_standard CRASH: {e}")
        DEBUG_STATS["parse_xml_failed"] += 1
        
        # Strategy 2: keyword
        try:
            r = parse_xml_keyword(doc["text"])
            if r is not None:
                r["purchases"] = validate_purchases(r["purchases"])
                DEBUG_STATS["parse_success"] += 1
                return r
        except Exception as e:
            logger.warning(f"parse_xml_keyword CRASH: {e}")
        DEBUG_STATS["parse_keyword_failed"] += 1
        
        # Strategy 3: regex
        try:
            r = parse_xml_regex(doc["text"])
            if r is not None:
                r["purchases"] = validate_purchases(r["purchases"])
                DEBUG_STATS["parse_success"] += 1
                return r
        except Exception as e:
            logger.warning(f"parse_xml_regex CRASH: {e}")
        DEBUG_STATS["parse_regex_failed"] += 1
        
        # Tutti falliti — log preview del contenuto per debug
        preview = doc["text"][:300] if doc.get("text") else "(empty)"
        logger.warning(f"ALL PARSERS FAILED. Preview: {preview!r}")
        return None
    
    elif doc["type"] == "html":
        try:
            r = parse_html_table(doc["text"])
            if r is not None:
                r["purchases"] = validate_purchases(r["purchases"])
                DEBUG_STATS["parse_success"] += 1
                return r
        except Exception as e:
            logger.warning(f"parse_html_table CRASH: {e}")
        DEBUG_STATS["parse_html_failed"] += 1
        return None


def detect_clusters(matches: list[dict], min_insiders: int = 3) -> None:
    groups = {}
    for m in matches:
        key = (m["ticker"], m["date"])
        groups.setdefault(key, set()).add(m["insider_name"])
    
    for m in matches:
        key = (m["ticker"], m["date"])
        if len(groups.get(key, set())) >= min_insiders:
            m["is_cluster"] = True
            m["cluster_size"] = len(groups[key])
        else:
            m["is_cluster"] = False
            m["cluster_size"] = 0


def parallel_map(fn, items, max_workers: int = 5, progress_callback=None):
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fn, item): item for item in items}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                results.append(future.result())
            except Exception:
                results.append(None)
            if progress_callback:
                progress_callback(i, len(items))
    return results
