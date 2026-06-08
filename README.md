# DV Job Alerts

Automated email alerts for **Design Verification / RTL / computer architecture**
openings at top US semiconductor and AI-hardware companies.

It polls each company's public job-board API, keeps only postings whose title
matches your keywords (verification, RTL, Verilog, UVM, SystemVerilog, computer
architecture, DV), and emails you whenever a **new** one appears. Runs free on a
GitHub Actions cron — no server, nothing to keep running on your laptop.

## Companies monitored

| Type | Companies |
|------|-----------|
| Greenhouse | Tenstorrent (incl. University board), Cerebras, SambaNova, Lightmatter |
| Ashby | Etched, d-Matrix |
| Lever | Mythic |
| Workday | NVIDIA, Intel, Broadcom, Marvell, Micron |

> **AMD, Qualcomm, Apple, Google** block scripted access to their boards, so set
> up a LinkedIn alert for those (see bottom). Everything else is automated here.

## How it works

- `monitor.py` — the scraper + matcher + emailer (Python stdlib only, no installs).
- `config.json` — the company list, your email, and the keywords. Edit freely.
- `state/seen.json` — remembers which jobs it has already told you about, so you
  only get alerts for genuinely new postings. Created automatically.
- `.github/workflows/check-jobs.yml` — runs `monitor.py` every 6 hours and commits
  the updated state back to the repo.

The **first run** emails you all currently-open matches (so you get the live list
immediately); after that you only get new ones.

## Setup (one time, ~10 minutes)

### 1. Create a Gmail App Password (for sending the email)
1. Use/ create a Gmail account to send *from* (can be the same as the recipient).
2. Enable 2-Step Verification: https://myaccount.google.com/security
3. Create an App Password: https://myaccount.google.com/apppasswords → pick "Mail".
4. Copy the 16-character password (you'll paste it as `SMTP_PASS`).

> Prefer not to use Gmail? Any SMTP server works — set `SMTP_HOST`/`SMTP_PORT`.

### 2. Push this folder to a new GitHub repo
```bash
cd dv-job-alerts
git init
git add .
git commit -m "Initial commit: DV job alerts"
git branch -M main
git remote add origin https://github.com/<your-username>/dv-job-alerts.git
git push -u origin main
```

### 3. Add your secrets in GitHub
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add:

| Secret | Value |
|--------|-------|
| `SMTP_USER` | the Gmail you send from, e.g. `you@gmail.com` |
| `SMTP_PASS` | the 16-char app password from step 1 |
| `ALERT_TO`  | where alerts go — `hnarra@usc.edu` |

(`SMTP_HOST`/`SMTP_PORT` are optional; default to Gmail's `smtp.gmail.com:587`.)

### 4. Turn it on
Repo → **Actions** tab → enable workflows → open **"DV job alerts"** →
**Run workflow** to trigger the first run immediately. After that it runs every
6 hours automatically.

## Run it locally (optional)

```bash
# See what currently matches without sending email or changing state:
python monitor.py --dry-run

# Real run, but print instead of emailing:
python monitor.py --no-email

# Real run with email (set the env vars first):
#   PowerShell:  $env:SMTP_USER="you@gmail.com"; $env:SMTP_PASS="app-pw"; $env:ALERT_TO="hnarra@usc.edu"
python monitor.py
```

## Customizing

Edit `config.json`:

- **`match_terms`** — title keywords (lowercased substring match). Add/remove to
  widen or narrow. e.g. add `"fpga"`, `"asic"`, `"validation"`.
- **`us_only`** — `true` drops obviously non-US locations. Set `false` for global.
- **`sources`** — add more companies. Greenhouse/Ashby/Lever just need the board
  `slug`; Workday needs `host`, `tenant`, and `site`.
- **Schedule** — change the `cron` line in the workflow (it's in UTC).

## Fallback for AMD / Qualcomm / Apple / Google (LinkedIn alert)

These block API scraping. Set a single LinkedIn alert that covers all of them:
1. LinkedIn → Jobs → search **"Design Verification"**, location **United States**.
2. Open the search, toggle **"Set alert"** (top of results), choose daily/weekly.
3. Optionally filter by company. This emails you new matching postings directly.

Also worth enabling: USC **Handshake** saved-search alerts for "verification".
