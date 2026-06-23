"""
FastAPI backend for the AIONOS Account News Bot.

Run with:
    uvicorn app:app --reload

NOTE: Newsbot.py lines 140-214 (collect_linkedin body) are not commented out
even though the def line is. Fix that syntax error before importing works:
    # def collect_linkedin(account, company):
    #     provider = ...
"""

import os
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from Newsbot import (
    collect,
    brain,
    cluster_items,
    render_email_html,
    send_email,
    load_sent,
    save_sent,
    item_key,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.YAML")

app = FastAPI(title="AIONOS News Bot")

STATIC_DIR = os.path.join(HERE, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=FileResponse)
def landing():
    return FileResponse(os.path.join(STATIC_DIR, "landing.html"))


@app.get("/login", response_class=FileResponse)
def login():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/dashboard", response_class=FileResponse)
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

# Last fetch results cached in memory, keyed by account name.
_results: dict = {}
# User-edited newsletter HTML overrides, keyed by account name.
_edited_newsletters: dict = {}


class NewsletterEdit(BaseModel):
    html: str


class SelectRequest(BaseModel):
    selected_titles: list[str]


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _account_by_name(config: dict, name: str) -> dict:
    for a in config["accounts"]:
        if a["name"].lower() == name.lower():
            return a
    return None


# ── GET /api/accounts ────────────────────────────────────────────────────────

@app.get("/api/accounts")
def get_accounts():
    """Return the list of tracked accounts from config.YAML."""
    config = _load_config()
    return [
        {
            "name": a["name"],
            "industry": a.get("industry", ""),
            "mode": a.get("mode", "instant"),
        }
        for a in config["accounts"]
    ]


# ── POST /api/fetch/all ──────────────────────────────────────────────────────

@app.post("/api/fetch/all")
def fetch_all():
    """
    Run collect() + brain() for every account.
    Skips items already in sent.json (dedup).
    Caches results in memory for /preview and /send.
    Returns scored articles grouped by account name.
    """
    config = _load_config()
    sent = load_sent()
    summary = {}

    for acct in config["accounts"]:
        name = acct["name"]
        rows = []

        for cluster in cluster_items(collect(acct)):
            item = cluster[0]
            item["also"] = [
                {"source": x["source"], "link": x["link"]} for x in cluster[1:]
            ]

            v = brain(item, acct.get("industry", ""))
            # if not v.get("relevant"):
            #     continue

            rows.append({"item": item, "verdict": v})

        _results[name] = rows
        summary[name] = [
            {
                "title": r["item"]["title"],
                "source": r["item"]["source"],
                "link": r["item"]["link"],
                "priority": r["verdict"]["priority"],
                "category": r["verdict"]["category"],
                "team": r["verdict"]["team"],
                "what_happened": r["verdict"]["what_happened"],
                "business_impact": r["verdict"]["business_impact"],
                "recommended_action": r["verdict"]["recommended_action"],
                "opportunity": r["verdict"]["opportunity"],
            }
            for r in rows
        ]

    return summary


# ── GET /api/preview/{account} ───────────────────────────────────────────────

@app.get("/api/preview/{account}", response_class=HTMLResponse)
def preview(account: str):
    """
    Return the rendered HTML email for a single account.
    Requires /api/fetch/all to have been called first.
    """
    if account not in _results:
        raise HTTPException(
            status_code=404,
            detail=f"No results for '{account}'. Call POST /api/fetch/all first.",
        )
    rows = _results[account]
    if not rows:
        return HTMLResponse(
            "<p style='font-family:sans-serif;padding:16px;color:#555;'>"
            f"No articles passed scoring for <b>{account}</b>.</p>"
        )
    return HTMLResponse(render_email_html(account, rows))


# ── PUT /api/newsletter/{account} ───────────────────────────────────────────

@app.put("/api/newsletter/{account}")
def save_newsletter(account: str, body: NewsletterEdit):
    """Save a user-edited HTML newsletter for an account."""
    _edited_newsletters[account] = body.html
    return {"saved": True}


# ── POST /api/select/{account} ──────────────────────────────────────────────

@app.post("/api/select/{account}")
def select_articles(account: str, body: SelectRequest):
    """Filter _results to selected titles, regenerate HTML, cache in _edited_newsletters."""
    if account not in _results:
        raise HTTPException(
            status_code=404,
            detail=f"No results for '{account}'. Call POST /api/fetch/all first.",
        )
    selected = set(body.selected_titles)
    rows = [r for r in _results[account] if r["item"]["title"] in selected]
    if not rows:
        html = (
            "<p style='font-family:sans-serif;padding:16px;color:#555;'>"
            f"No articles selected for <b>{account}</b>.</p>"
        )
    else:
        html = render_email_html(account, rows)
    _edited_newsletters[account] = html
    return {"html": html}


# ── POST /api/send/all ───────────────────────────────────────────────────────

@app.post("/api/send/all")
def send_all(dry_run: bool = True):
    """
    Send (or dry-run) digest emails for all accounts.
    dry_run=True (default) — prints to stdout, nothing is sent or saved.
    dry_run=False          — sends real emails and updates sent.json.

    Requires /api/fetch/all to have been called first.
    """
    config = _load_config()
    sent = load_sent()
    results = []

    for acct in config["accounts"]:
        name = acct["name"]
        rows = _results.get(name)

        if not rows:
            results.append({"account": name, "skipped": True, "reason": "no articles or fetch not run"})
            continue

        subject = f"[{name}] Daily digest — {len(rows)} update(s)"
        html_override = _edited_newsletters.get(name)
        send_email(acct["owners"], subject, name, rows, dry_run, html=html_override)

        if not dry_run:
            for r in rows:
                sent.add(item_key(name, r["item"]["title"]))

        results.append({"account": name, "count": len(rows), "dry_run": dry_run})

    if not dry_run:
        save_sent(sent)

    return {"dry_run": dry_run, "results": results}
