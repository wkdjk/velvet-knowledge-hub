# Velvet Knowledge Hub

Quarterly deer velvet trade intelligence dashboard for New Zealand exporters.

Built with GitHub Actions + Google Sheets + GitHub Pages.

Live site: https://wkdjk.github.io/velvet-knowledge-hub/ (coming soon)

---

## Local CLI — ask.py

`ask.py` is a local-only companion script. It reads live data from the VKH Google Sheet and renders an interactive Plotly chart in the browser. It is not deployed and does not affect the website build.

### Setup

```bash
pip install plotly  # if not already installed via requirements.txt
```

Credentials: `GOOGLE_SERVICE_ACCOUNT_JSON` must be set in `.env` at the repo root (single-line JSON).

### Usage

```bash
# Trade flow — monthly NZ export volume line chart
PYTHONPATH=. python ask.py "뉴질랜드 수출 트렌드를 보여줘"
PYTHONPATH=. python ask.py "NZ export trend"

# Import records — top importers bar chart
PYTHONPATH=. python ask.py "2024 import records"
PYTHONPATH=. python ask.py "수입 기록 보여줘"

# News articles — browser table of 20 most recent articles
PYTHONPATH=. python ask.py "latest news"
PYTHONPATH=. python ask.py "최신 뉴스"

# List all tabs in the Sheet
PYTHONPATH=. python ask.py --list-tabs

# Help
PYTHONPATH=. python ask.py --help
```

### Question routing (v1 — keyword matching)

| Keywords | Handler | Chart type |
|----------|---------|------------|
| trade, export, 수출, nz, 뉴질랜드 | Trade flow | Line chart — VTW_Trade_Monthly (KG) |
| import, 수입, mfds, record, food | Import records | Bar chart — VFI_Import_Records (top 20 importers) |
| news, 뉴스, article, 기사, latest | News summary | Console table + Plotly table — KVN_Articles |
