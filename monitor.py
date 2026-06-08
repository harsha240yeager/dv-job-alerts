#!/usr/bin/env python3
"""DV job-opening monitor.

Polls the public job-board APIs of a configured set of semiconductor / AI-hardware
companies, keeps only postings whose title matches verification / RTL / computer
architecture keywords, and emails any *new* matches since the last run.

Stdlib only (no pip install needed). Designed to run on a GitHub Actions cron.

Email is sent via SMTP using these environment variables:
    SMTP_HOST   (default: smtp.gmail.com)
    SMTP_PORT   (default: 587)
    SMTP_USER   (the sending address, e.g. your Gmail)
    SMTP_PASS   (an app password, NOT your normal password)
    ALERT_TO    (recipient; falls back to config.json "alert_to")

Run modes:
    python monitor.py            # normal: fetch, email new matches, update state
    python monitor.py --dry-run  # fetch + print matches, send NO email, no state change
    python monitor.py --no-email # fetch + update state, print instead of emailing
"""

import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"

UA = "Mozilla/5.0 (compatible; dv-job-alerts/1.0)"
TIMEOUT = 30

# Locations containing any of these markers are treated as non-US and dropped
# when us_only is enabled. Anything not matching is kept (US or ambiguous).
NON_US_MARKERS = [
    "india", "bengaluru", "bangalore", "hyderabad", "noida", "pune", "chennai",
    "taiwan", "taipei", "hsinchu", "tainan", "china", "shanghai", "beijing",
    "shenzhen", "canada", "toronto", "ontario", "vancouver", "markham",
    "israel", "haifa", "tel aviv", "serbia", "belgrade", "beograd",
    "germany", "munich", "münchen", "uk", "united kingdom", "england",
    "ireland", "dublin", "japan", "tokyo", "korea", "seoul", "singapore",
    "malaysia", "penang", "philippines", "vietnam", "mexico", "guadalajara",
    "brazil", "poland", "romania", "france", "netherlands", "spain",
    "australia", "sweden", "finland", "switzerland", "austria", "hungary",
]


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_seen():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=0)


def http_json(url, data=None, headers=None):
    hdrs = {"Accept": "application/json", "User-Agent": UA}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def title_matches(title, terms):
    t = (title or "").lower()
    return any(term in t for term in terms)


def is_us(location, us_only):
    if not us_only:
        return True
    loc = (location or "").lower()
    return not any(marker in loc for marker in NON_US_MARKERS)


# --- Source fetchers: each returns a list of dicts {id,title,location,url,company} ---

def fetch_greenhouse(src):
    url = f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs"
    data = http_json(url)
    out = []
    for j in data.get("jobs", []):
        out.append({
            "id": f"gh:{src['slug']}:{j['id']}",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "company": src["company"],
        })
    return out


def fetch_ashby(src):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}"
    data = http_json(url)
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({
            "id": f"ashby:{src['slug']}:{j['id']}",
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", ""),
            "company": src["company"],
        })
    return out


def fetch_lever(src):
    url = f"https://api.lever.co/v0/postings/{src['slug']}?mode=json"
    data = http_json(url)
    out = []
    for j in data:
        cats = j.get("categories", {}) or {}
        out.append({
            "id": f"lever:{src['slug']}:{j.get('id')}",
            "title": j.get("text", ""),
            "location": cats.get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "company": src["company"],
        })
    return out


def fetch_workday(src, search_terms):
    """Workday cxs endpoint. Runs one search per term, dedupes by id."""
    base = f"https://{src['host']}/wday/cxs/{src['tenant']}/{src['site']}"
    public = f"https://{src['host']}/en-US/{src['site']}"
    found = {}
    for term in search_terms:
        try:
            data = http_json(f"{base}/jobs", data={
                "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term,
            })
        except urllib.error.URLError:
            continue
        for j in data.get("jobPostings", []):
            path = j.get("externalPath", "")
            jid = (j.get("bulletFields") or [path])[0]
            found[jid] = {
                "id": f"wd:{src['tenant']}:{jid}",
                "title": j.get("title", ""),
                "location": j.get("locationsText", "") or "",
                "url": f"{public}{path}",
                "company": src["company"],
            }
        time.sleep(0.3)  # be polite
    return list(found.values())


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
}


def collect(cfg):
    terms = [t.lower() for t in cfg["match_terms"]]
    wd_terms = cfg.get("workday_search_terms", ["verification"])
    us_only = cfg.get("us_only", True)
    matches = []
    for src in cfg["sources"]:
        typ = src["type"]
        try:
            if typ == "workday":
                jobs = fetch_workday(src, wd_terms)
            else:
                jobs = FETCHERS[typ](src)
        except Exception as e:  # noqa: BLE001 - keep monitor resilient per-source
            print(f"  ! {src['company']} ({typ}) failed: {e}", file=sys.stderr)
            continue
        kept = [j for j in jobs
                if title_matches(j["title"], terms) and is_us(j["location"], us_only)]
        print(f"  {src['company']}: {len(kept)} matching of {len(jobs)} fetched")
        matches.extend(kept)
    # dedupe by id
    dedup = {j["id"]: j for j in matches}
    return list(dedup.values())


def render_email(jobs, cfg=None):
    by_company = {}
    for j in sorted(jobs, key=lambda x: (x["company"], x["title"])):
        by_company.setdefault(j["company"], []).append(j)
    lines = [f"{len(jobs)} matching DV / RTL / verification opening(s):", ""]
    for company, items in sorted(by_company.items()):
        lines.append(f"=== {company} ({len(items)}) ===")
        for j in items:
            loc = f"  [{j['location']}]" if j["location"] else ""
            lines.append(f"  - {j['title']}{loc}")
            lines.append(f"    {j['url']}")
        lines.append("")

    manual = (cfg or {}).get("manual_companies") or []
    if manual:
        lines.append("-" * 60)
        lines.append("NOT auto-monitored - check these companies manually:")
        lines.append("  " + ", ".join(manual))
        note = (cfg or {}).get("manual_note")
        if note:
            lines.append(f"  {note}")
        lines.append("")
    return "\n".join(lines)


def send_email(subject, body, alert_to):
    # Use `or` (not the get default) so empty-string env vars also fall back -
    # GitHub Actions injects unset secrets as "".
    user = os.environ.get("SMTP_USER") or ""
    password = os.environ.get("SMTP_PASS") or ""
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    to = os.environ.get("ALERT_TO") or alert_to
    # Some providers (Brevo, SendGrid, Mailjet) use a login that is NOT a valid
    # "From" address, so allow an explicit sender via EMAIL_FROM.
    sender = os.environ.get("EMAIL_FROM") or user
    if not (user and password and to):
        print("!! SMTP_USER / SMTP_PASS / recipient not set; skipping email.",
              file=sys.stderr)
        print(body)
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP(host, port, timeout=TIMEOUT) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(sender, [to], msg.as_string())
    print(f"Emailed {to}.")
    return True


def main():
    dry_run = "--dry-run" in sys.argv
    no_email = "--no-email" in sys.argv

    cfg = load_config()
    print("Fetching sources...")
    jobs = collect(cfg)
    print(f"Total matching (deduped): {len(jobs)}")

    seen = load_seen()
    first_run = not STATE_PATH.exists()
    new_jobs = [j for j in jobs if j["id"] not in seen]
    print(f"New since last run: {len(new_jobs)}"
          + (" (first run - reporting all current matches)" if first_run else ""))

    if dry_run:
        print("\n--- DRY RUN (no email, no state change) ---")
        print(render_email(jobs if first_run else new_jobs, cfg))
        return

    if new_jobs:
        when = "current open" if first_run else "NEW"
        subject = f"[DV Jobs] {len(new_jobs)} {when} opening(s)"
        body = render_email(new_jobs, cfg)
        if no_email:
            print(body)
        else:
            send_email(subject, body, cfg.get("alert_to", ""))
    else:
        print("No new matches; no email sent.")

    seen.update(j["id"] for j in jobs)
    save_seen(seen)
    print(f"State saved ({len(seen)} ids tracked).")


if __name__ == "__main__":
    main()
