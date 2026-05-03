# OS Insider Scanner

Scansione SEC EDGAR Form 4 P+A · Biotech / Tech / Semi / MedDevice

## ⚠️ STRUTTURA REPO CORRETTA

I file devono stare alla **ROOT del repository GitHub**. Streamlit Cloud cerca `app.py` lì.

```
TUO_REPO/
├── app.py                  ← qui!
├── edgar.py
├── requirements.txt
├── runtime.txt
├── README.md
├── .gitignore
└── .streamlit/
    ├── config.toml
    └── secrets.toml.example
```

**NON** mettere i file in una sottocartella tipo `insider-scanner-app/`.

## 🚀 Deploy passo passo

### Se hai già un repo P-SEC con struttura sbagliata

Vai sul repo GitHub:
1. **Cancella** il repo P-SEC esistente: `Settings → Danger Zone → Delete this repository`
2. Crea repo **nuovo** chiamato `insider-scanner`
3. Carica i file di QUESTO zip nella ROOT (drag-and-drop diretto)

### Se parti da zero

1. Crea repo GitHub `insider-scanner` (privato consigliato)
2. Estrai questo zip
3. Trascina TUTTI i file dentro il repo (in root!)
4. Commit

### Streamlit Cloud

1. [share.streamlit.io](https://share.streamlit.io) → New app
2. Repo: `insider-scanner`, Branch: `main`, Main file: **`app.py`** (in root!)
3. Click Deploy
4. Settings → Secrets:
```toml
SEC_EMAIL = "oscar.gioffre@gmail.com"
TELEGRAM_BOT_TOKEN = "il_tuo_token"
TELEGRAM_CHAT_ID = "il_tuo_chat_id"
```

## 🛡️ Rate limiting fixed

L'errore "rate limit 429" della versione precedente era dovuto a richieste troppo aggressive verso `data.sec.gov`. Questa versione ha un **token bucket separato per host**:

- `www.sec.gov`: 8 req/s (concorrenza 8)
- `data.sec.gov`: 4 req/s (concorrenza 4) ← molto più stretto

Le impostazioni di concorrenza sono regolabili dalla sidebar.

## ⚡ Performance attese

| Operazione | Tempo |
|-----------|-------|
| Prima scansione 24h (cache vuota) | 60-120s |
| Scansioni successive | 15-30s |
| Refresh ATOM | 10-20s |
| Solo dati nuovi | 3-8s |

## 🔧 Telegram

Vedi `.streamlit/secrets.toml.example`.

## 📊 Settori

| Settore | SIC |
|---------|-----|
| Biotech | 2836, 2834, 2833, 2835, 8731 |
| Technology | 7372, 7370, 7371, 7373, 7374, 7389 |
| Semiconductors | 3674, 3670, 3571, 3572, 3576, 3577 |
| MedDevice | 3841, 3845, 3826, 3827 |

Per modificare: edita `SIC_CODES` in `edgar.py`.
