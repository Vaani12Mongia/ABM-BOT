"""
FastAPI backend for the AIONOS Account News Bot.

Run with:
    uvicorn app:app --reload
"""

import os
import yaml
import hashlib
import threading
import atexit
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler

from newsbot import (
    collect,
    collect_scraped,
    brain,
    cluster_items,
    render_email_html,
    send_email,
    load_sent,
    save_sent,
    item_key,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")

# ── Auth config ──────────────────────────────────────────────────
# Add to your .env:
#   APP_USERS=vaani:password123,rishabh:password456
#   SESSION_SECRET=any-long-random-string

SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-in-production")

def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def _load_users() -> dict:
    raw = os.getenv("APP_USERS", "")
    users = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            username, password = pair.split(":", 1)
            users[username.strip()] = _hash_password(password.strip())
    return users

def _check_password(username: str, password: str) -> bool:
    users = _load_users()
    return users.get(username) == _hash_password(password)

def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("user"))

PUBLIC_PATHS = {"/auth/login", "/auth/logout", "/health", "/login"}

# ── Auth guard ───────────────────────────────────────────────────
# IMPORTANT: add_middleware stacks in reverse — last added = outermost = runs first.
# So SessionMiddleware must be added AFTER auth_guard to wrap it correctly.
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in PUBLIC_PATHS):
        return await call_next(request)
    if path.startswith("/static"):
        return await call_next(request)
    if not _is_authenticated(request):
        request.session["next_url"] = str(request.url)
        return RedirectResponse("/auth/login")
    return await call_next(request)

# ── App + Middleware ─────────────────────────────────────────────
app = FastAPI(title="AIONOS News Bot")
app.add_middleware(BaseHTTPMiddleware, dispatch=auth_guard)  # inner  — runs second
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)  # outer — runs first

# ── Shared state ─────────────────────────────────────────────────
_results: dict = {}
_edited_newsletters: dict = {}

# ── Config loader ────────────────────────────────────────────────
def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)

# ── Scheduler ────────────────────────────────────────────────────
def scheduled_fetch():
    """Runs every 6 hours — fetches and scores news for all accounts."""
    print(f"[Scheduler] Starting auto-fetch at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        config = _load_config()
        for acct in config["accounts"]:
            name = acct["name"]
            rows = []
            for cluster in cluster_items(collect(acct)):
                item = cluster[0]
                item["also"] = [{"source": x["source"], "link": x["link"]} for x in cluster[1:]]
                v = brain(item, acct.get("industry", ""))
                rows.append({"item": item, "verdict": v})
            _results[name] = rows
            print(f"[Scheduler] {name}: {len(rows)} articles scored")
        print(f"[Scheduler] Auto-fetch complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        print(f"[Scheduler] Error during auto-fetch: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_fetch, "interval", hours=6, id="auto_fetch")
scheduler.start()
print("[Scheduler] Started — will fetch news every 6 hours")

# Run once immediately on startup so news is ready right away
threading.Thread(target=scheduled_fetch, daemon=True).start()

# Shut down scheduler cleanly when app exits
atexit.register(lambda: scheduler.shutdown())

# ── Auth routes ──────────────────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    error_html = f"<p style='color:red;margin:0 0 12px;font-size:14px;'>{error}</p>" if error else ""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>AIONOS News Bot — Login</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f4f8;display:flex;align-items:center;justify-content:center;min-height:100vh}}
    .card{{background:white;border-radius:12px;padding:40px;width:360px;box-shadow:0 4px 24px rgba(0,0,0,0.08)}}
    .logo{{font-size:22px;font-weight:700;color:#1a1a2e;margin-bottom:6px}}
    .subtitle{{font-size:13px;color:#888;margin-bottom:28px}}
    label{{display:block;font-size:13px;font-weight:500;color:#444;margin-bottom:6px}}
    input{{width:100%;padding:10px 14px;border:1px solid #ddd;border-radius:8px;font-size:14px;margin-bottom:16px;outline:none}}
    input:focus{{border-color:#4f46e5}}
    button{{width:100%;padding:11px;background:#4f46e5;color:white;border:none;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}}
    button:hover{{background:#4338ca}}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">AIONOS News Bot</div>
    <div class="subtitle">Account Intelligence Dashboard</div>
    {error_html}
    <form method="post" action="/auth/login">
      <label>Username</label>
      <input type="text" name="username" placeholder="Enter username" required autofocus/>
      <label>Password</label>
      <input type="password" name="password" placeholder="Enter password" required/>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>""")

@app.post("/auth/login")
async def login_submit(request: Request):
    form = await request.form()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()
    if not _check_password(username, password):
        return RedirectResponse("/auth/login?error=Invalid+username+or+password", status_code=303)
    request.session["user"] = {"username": username}
    next_url = request.session.pop("next_url", "/dashboard")
    return RedirectResponse(next_url, status_code=303)

@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login")

@app.get("/auth/me")
async def auth_me(request: Request):
    return JSONResponse(request.session.get("user") or {})

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/login")
async def login_redirect():
    return RedirectResponse("/auth/login")

# ── Scheduler status endpoint ────────────────────────────────────
@app.get("/api/scheduler/status")
def scheduler_status():
    """Check when the last and next fetch ran."""
    job = scheduler.get_job("auto_fetch")
    return {
        "scheduler": "running" if scheduler.running else "stopped",
        "next_run": str(job.next_run_time) if job else "unknown",
        "accounts_loaded": list(_results.keys()),
        "article_counts": {k: len(v) for k, v in _results.items()},
    }

# ════════════════════════════════════════════════════════════════
# EXISTING ROUTES — all protected by auth_guard
# ════════════════════════════════════════════════════════════════

STATIC_DIR = os.path.join(HERE, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=FileResponse)
def landing():
    return FileResponse(os.path.join(STATIC_DIR, "landing.html"))

@app.get("/dashboard", response_class=FileResponse)
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

class NewsletterEdit(BaseModel):
    html: str

class SelectRequest(BaseModel):
    selected_titles: list[str]

def _account_by_name(config: dict, name: str) -> dict:
    for a in config["accounts"]:
        if a["name"].lower() == name.lower():
            return a
    return None

@app.get("/api/accounts")
def get_accounts():
    config = _load_config()
    return [{"name": a["name"], "industry": a.get("industry", ""), "mode": a.get("mode", "instant")} for a in config["accounts"]]

@app.get("/api/scrape-test/{account}")
def scrape_test(account: str):
    """Debug endpoint: run collect_scraped() for one account (no AI scoring)."""
    config = _load_config()
    acct = _account_by_name(config, account)
    if acct is None:
        raise HTTPException(status_code=404, detail=f"Account '{account}' not found.")
    items = collect_scraped(acct)
    return {"account": acct["name"], "items": items, "count": len(items)}

@app.post("/api/fetch/all")
def fetch_all():
    config = _load_config()
    sent = load_sent()
    summary = {}
    for acct in config["accounts"]:
        name = acct["name"]
        rows = []
        for cluster in cluster_items(collect(acct)):
            item = cluster[0]
            item["also"] = [{"source": x["source"], "link": x["link"]} for x in cluster[1:]]
            v = brain(item, acct.get("industry", ""))
            rows.append({"item": item, "verdict": v})
        _results[name] = rows
        summary[name] = [
            {
                "title": r["item"]["title"],
                "title_en": r["verdict"].get("title_en", r["item"]["title"]),
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

@app.get("/api/preview/{account}", response_class=HTMLResponse)
def preview(account: str):
    if account not in _results:
        raise HTTPException(status_code=404, detail=f"No results for '{account}'. Call POST /api/fetch/all first.")
    rows = _results[account]
    if not rows:
        return HTMLResponse(f"<p style='font-family:sans-serif;padding:16px;color:#555;'>No articles passed scoring for <b>{account}</b>.</p>")
    return HTMLResponse(render_email_html(account, rows))

@app.put("/api/newsletter/{account}")
def save_newsletter(account: str, body: NewsletterEdit):
    _edited_newsletters[account] = body.html
    return {"saved": True}

@app.post("/api/select/{account}")
def select_articles(account: str, body: SelectRequest):
    if account not in _results:
        raise HTTPException(status_code=404, detail=f"No results for '{account}'. Call POST /api/fetch/all first.")
    selected = set(body.selected_titles)
    rows = [r for r in _results[account] if r["item"]["title"] in selected]
    html = render_email_html(account, rows) if rows else f"<p style='font-family:sans-serif;padding:16px;color:#555;'>No articles selected for <b>{account}</b>.</p>"
    _edited_newsletters[account] = html
    return {"html": html}

@app.post("/api/send/all")
def send_all(request: Request, dry_run: bool = True):
    user = request.session.get("user", {})
    triggered_by = user.get("username", "unknown")
    config = _load_config()
    sent = load_sent()
    results = []
    for acct in config["accounts"]:
        name = acct["name"]
        rows = _results.get(name)
        if not rows:
            results.append({"account": name, "skipped": True, "reason": "no articles or fetch not run"})
            continue
        subject = f"[{name}] Account Intelligence Digest"
        html_override = _edited_newsletters.get(name)
        send_email(acct["owners"], subject, name, rows, dry_run, html=html_override)
        if not dry_run:
            for r in rows:
                sent.add(item_key(name, r["item"]["title"]))
        results.append({"account": name, "count": len(rows), "dry_run": dry_run})
    if not dry_run:
        save_sent(sent)
    return {"dry_run": dry_run, "triggered_by": triggered_by, "results": results}

@app.get("/api/results")
def get_results():
    """Return already-fetched results from scheduler without re-fetching."""
    summary = {}
    for name, rows in _results.items():
        summary[name] = [
            {
                "title": r["item"]["title"],
                "title_en": r["verdict"].get("title_en", r["item"]["title"]),
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