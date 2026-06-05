# Six10 Pricing Strategy Engine — Setup Guide

## Prerequisites

- Python 3.10+
- Access to the shared `Data Feeds` folder (place in project root)
- `pricing-strategy-engine-4983eab4d919.json` — service account key (for Google Sheets)
- `credentials-gmail-pricing.json` — OAuth 2.0 client credentials (for Gmail email)

Both JSON files should already be in the project root.

---

## 1. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 2. Run Without Google Sheets (Local Excel Mode)

No credentials needed. All output goes to a local Excel file:

```bash
python main.py --dry-run --output-local test_output.xlsx
```

Open `test_output.xlsx` to verify all 4 tabs: Summary, Full Catalog, History, Config.

---

## 3. Google Sheets — Already Configured

The sheet ID and service account path are hardcoded in `config.yaml`:

```yaml
google_sheets:
  sheet_id: "1xtyF6SDcQNA5H1wJ9itQm6WIHN0_NL9DAO7DA44NaEA"
  credentials_path: "pricing-strategy-engine-4983eab4d919.json"
```

### One required step: share the sheet with the service account

1. Open `pricing-strategy-engine-4983eab4d919.json` and copy the `client_email` value
   (looks like `pricing-strategy-engine@<project>.iam.gserviceaccount.com`)
2. Open the Google Sheet
3. Click **Share** → paste the service account email → set role to **Editor** → Send

That's it. The engine will find the sheet by ID — no sheet name needed. All 4 tabs
(Summary, Full Catalog, History, Config) are created automatically on first run.

### Run with Sheets output

```bash
python main.py --no-email
```

---

## 4. Gmail Email Alerts — One-Time OAuth Consent

The engine sends email via the **Gmail API with OAuth 2.0**.  
No SMTP, no App Password, no admin access, no 2-step verification required.

### How it works

| File | Purpose |
|------|---------|
| `credentials-gmail-pricing.json` | OAuth client credentials (already in project root) |
| `gmail_token.json` | Stored OAuth token — **created once** by the step below |

Once `gmail_token.json` exists, the engine sends email silently on every run.
The token auto-refreshes when it expires — no re-consent needed.

### Step 1 — Run the consent script (once only)

```bash
python authorize_gmail.py
```

This will:
1. Open your browser → Google sign-in page
2. Log in as **ai@six10ventures.com**
3. Click **Allow** (grants "Send email on your behalf" — read access is not requested)
4. Browser closes → `gmail_token.json` is saved in the project root

### Step 2 — Run the full engine

```bash
python main.py
```

Email will be sent from `ai@six10ventures.com` to `shashank@six10ventures.com`.

### Changing recipients

Edit `config.yaml`:

```yaml
email:
  sender: "ai@six10ventures.com"
  recipients:
    - "shashank@six10ventures.com"
    - "another@six10ventures.com"
```

### Re-authorising

Only needed if `gmail_token.json` is deleted or you see `"Failed to refresh Gmail token"` in logs:

```bash
del gmail_token.json          # Windows
python authorize_gmail.py     # re-run consent
```

---

## 5. Weekly Scheduled Run

### Windows Task Scheduler (no env vars needed — paths are in config.yaml)

```batch
@echo off
cd /d "C:\path\to\Pricing strategy Engine"
python main.py >> pricing_engine.log 2>&1
```

1. Open **Task Scheduler** → Create Basic Task
2. Trigger: **Weekly**, pick day + time
3. Action: **Start a program** → `python` → Arguments: `main.py`
4. Start in: full path to project folder

---

## 6. CLI Reference

| Command | Description |
|---------|-------------|
| `python main.py` | Full run: compute → Sheets → email |
| `python main.py --dry-run` | Compute only, no writes, no email |
| `python main.py --no-email` | Write to Sheets, skip email |
| `python main.py --output-local OUT.xlsx` | Write to local Excel (combinable with any flag) |
| `python main.py --dry-run --output-local test.xlsx` | Safe test run, output to Excel |
| `python authorize_gmail.py` | One-time Gmail OAuth consent |

---

## 7. Data Feed Folder Structure

```
Data Feeds/
├── SellerRise Sales Data/
│   ├── AquaDoc/
│   ├── Mokita Naturals/
│   ├── NL Brands (PawMedica)/
│   ├── Pureauty Naturals/
│   └── Visivite/
├── Amazon FBA Inventory report/
│   ├── AquaDoc/
│   ├── Mokita Naturals/
│   ├── NL Brands (PawMedica)/
│   ├── Pureauty Naturals/
│   └── Visivite/
├── Amazon Sales and Traffic report/
│   └── AquaDocBusinessReport-*.csv
├── COGS Sheet.xlsx
└── Product Families.xlsx
```

---

## 8. Troubleshooting

| Problem | Fix |
|---------|-----|
| `gmail_token.json not found` | Run `python authorize_gmail.py` |
| `Failed to refresh Gmail token` | Delete `gmail_token.json`, re-run `authorize_gmail.py` |
| `HttpError 403` on Gmail send | Ensure Gmail API is enabled in Google Cloud Console for the OAuth client project |
| `gspread SpreadsheetNotFound` | Share the sheet with the service account `client_email` (Editor access) |
| `FileNotFoundError: Data Feeds/...` | Ensure `Data Feeds` folder is in the project root |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| All ASINs are HOLD | Check `pricing_engine.log` for COGS missing warnings |

Logs are written to `pricing_engine.log` in the project root.
