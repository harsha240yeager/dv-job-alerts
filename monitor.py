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

import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "seen.json"

UA = "Mozilla/5.0 (compatible; dv-job-alerts/1.0)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 30

# Title must contain one of these (word-boundary) when intern_only is enabled.
INTERN_RE = re.compile(r"\b(intern|interns|internship|co-?op)\b", re.IGNORECASE)

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


def is_intern(title):
    # Word-boundary so "Internal IP" etc. is NOT treated as an internship.
    return bool(INTERN_RE.search(title or ""))


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


def _clean(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _linkedin_search(query, location, tpr):
    """Returns list of parsed cards: {id,title,company,location,url}."""
    params = urllib.parse.urlencode({
        "keywords": query, "location": location, "f_TPR": tpr, "start": 0,
    })
    req = urllib.request.Request(
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + params,
        headers={"User-Agent": BROWSER_UA, "Accept": "text/html"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    cards = []
    # Each job card starts at a base-card div; split on it so fields stay aligned.
    for chunk in re.split(r'<div class="base-card', page)[1:]:
        urn = re.search(r'urn:li:jobPosting:(\d+)', chunk)
        if not urn:
            continue
        jid = urn.group(1)
        title_m = re.search(r'base-search-card__title">\s*(.*?)\s*</h3>', chunk, re.S)
        comp_m = re.search(r'base-search-card__subtitle">\s*(.*?)\s*</h4>', chunk, re.S)
        loc_m = re.search(r'job-search-card__location">\s*(.*?)\s*</span>', chunk, re.S)
        link_m = re.search(r'href="(https://[^"]*?/jobs/view/[^"]+?)"', chunk)
        cards.append({
            "id": jid,
            "title": _clean(title_m.group(1)) if title_m else "",
            "company": _clean(comp_m.group(1)) if comp_m else "",
            "location": _clean(loc_m.group(1)) if loc_m else location,
            "url": (link_m.group(1).split("?")[0]
                    if link_m else f"https://www.linkedin.com/jobs/view/{jid}"),
        })
    return cards


def fetch_linkedin(src):
    """LinkedIn public 'jobs-guest' search (no login) to cover companies whose
    own boards block automation (AMD, Qualcomm, ...).

    Queries each company by name and keeps only cards whose company actually
    matches, so unrelated fuzzy results are discarded. Best-effort: LinkedIn may
    rate-limit datacenter IPs, in which case the source is skipped for that run.
    """
    location = src.get("location", "United States")
    tpr = src.get("posted_within", "r2592000")  # default: last 30 days
    companies = src.get("companies", [])
    roles = src.get("role_queries", ["verification intern"])
    found = {}

    def add(card, label):
        found[card["id"]] = {
            "id": f"li:{card['id']}",
            "title": card["title"],
            "location": card["location"],
            "url": card["url"],
            "company": f"{label} (via LinkedIn)",
        }

    targets = companies if companies else [None]
    for company in targets:
        for role in roles:
            query = f"{company} {role}" if company else role
            try:
                cards = _linkedin_search(query, location, tpr)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                continue
            for card in cards:
                if company:
                    if company.lower() not in card["company"].lower():
                        continue
                    add(card, company)
                else:
                    add(card, card["company"] or "LinkedIn")
            time.sleep(0.4)
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
    intern_only = cfg.get("intern_only", False)
    matches = []
    for src in cfg["sources"]:
        typ = src["type"]
        try:
            if typ == "workday":
                jobs = fetch_workday(src, wd_terms)
            elif typ == "linkedin":
                jobs = fetch_linkedin(src)
            else:
                jobs = FETCHERS[typ](src)
        except Exception as e:  # noqa: BLE001 - keep monitor resilient per-source
            print(f"  ! {src['company']} ({typ}) failed: {e}", file=sys.stderr)
            continue
        kept = [j for j in jobs
                if title_matches(j["title"], terms)
                and is_us(j["location"], us_only)
                and (not intern_only or is_intern(j["title"]))]
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
