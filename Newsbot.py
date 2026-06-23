#!/usr/bin/env python3
"""
Account News Bot — AIONOS edition.

Flow:
  collect (RSS + NewsData broad + NewsData company-site + LinkedIn)
   -> clean URLs -> CLUSTER duplicates -> dedupe memory -> AI score
   -> drop noise/LOW -> route to owner -> send / digest / hold

LANGUAGE: news is collected in ALL languages (no language filter). The AI agent
translates every field — including the headline (title_en) — into English, so
emails are always fully English regardless of the source language.

AI providers (set AI_PROVIDER in .env):
  none / anthropic / azure / azure_agent
  (Real providers HARD-FAIL on error instead of silently using keyword fallback.)

azure_agent .env keys (Microsoft Foundry prompt agent via Responses API):
  AI_PROVIDER=azure_agent
  AZURE_AI_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
  AZURE_AI_AGENT_NAME=trial
  AZURE_AI_AGENT_VERSION=4      # bump after you update the agent's Instructions

NewsData.io — TWO keys (free keys at https://newsdata.io):
  NEWSDATA_API_KEY_ALERT     -> broad news search across all outlets
  NEWSDATA_API_KEY_WEBSITE   -> same query scoped to the company's own domain
                                (domainurl filter; PAID on NewsData, so the free
                                 tier may return little/nothing here)

Per-account config (config.yaml -> accounts[].sources):
  website_rss, alert_rss, news_query, company_domain, linkedin_company

Run:
  python Newsbot.py                 # normal run
  python Newsbot.py --dry-run       # never send email, just print
"""

import os, sys, re, json, argparse, hashlib, smtplib, html
from urllib.parse import urlparse, parse_qs
from difflib import SequenceMatcher
from email.message import EmailMessage
from datetime import datetime

import yaml
import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()
HERE = os.path.dirname(os.path.abspath(__file__))
SENT_FILE = os.path.join(HERE, "sent.json")
PENDING_DIR = os.path.join(HERE, "pending")

# ---------------------------------------------------------------- helpers
def log(msg, mark=" "):
    print(f"  {mark} {msg}")

def clean_url(u):
    """Unwrap Google Alert redirect links (google.com/url?...&url=REAL) into the real URL."""
    if not u:
        return u
    try:
        p = urlparse(u)
        if "google." in p.netloc and p.path.startswith("/url"):
            q = parse_qs(p.query)
            if q.get("url"):
                return q["url"][0]
    except Exception:
        pass
    return u

def item_key(account, title):
    raw = f"{account}|{title}".lower().strip()
    return hashlib.sha1(raw.encode()).hexdigest()

def load_sent():
    if os.path.exists(SENT_FILE):
        try:
            return set(json.load(open(SENT_FILE)))
        except Exception:
            return set()
    return set()

def save_sent(keys):
    json.dump(sorted(keys), open(SENT_FILE, "w"), indent=0)

def is_blank(url):
    return (not url) or "example" in url or "XXXX" in url

# ---------------------------------------------------------------- clustering
_STOP = {"the","a","an","at","in","on","of","to","for","and","with","is","are",
         "was","were","by","from","as","its","it","all","over","into","amid","has"}

def _title_tokens(t):
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return {w for w in t.split() if w not in _STOP and len(w) > 2}

def _similarity(t1, t2):
    a, b = _title_tokens(t1), _title_tokens(t2)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / min(len(a), len(b))
    seq = SequenceMatcher(None, t1.lower(), t2.lower()).ratio()
    return max(overlap, seq)

def cluster_items(items, threshold=0.5):
    clusters = []
    for it in items:
        best, target = 0.0, None
        for cl in clusters:
            s = max(_similarity(it["title"], m["title"]) for m in cl)
            if s > best:
                best, target = s, cl
        if target is not None and best >= threshold:
            target.append(it)
        else:
            clusters.append([it])
    for cl in clusters:
        cl.sort(key=lambda x: len(x.get("body", "")), reverse=True)
    return clusters

# ---------------------------------------------------------------- collectors
def collect_rss(url, source_label, account):
    if is_blank(url):
        log(f"{source_label}: not configured yet — skipping", "·")
        return []
    items = []
    try:
        feed = feedparser.parse(url)
        for e in feed.entries[:15]:
            items.append({
                "account": account,
                "source": source_label,
                "title": getattr(e, "title", "(no title)"),
                "link": clean_url(getattr(e, "link", "")),
                "body": getattr(e, "summary", "")[:600],
                "also": [],
            })
        log(f"{source_label}: {len(items)} item(s)")
    except Exception as ex:
        log(f"{source_label}: error reading feed ({ex})", "✗")
    return items

def _newsdata_query(api_key, query, source_label, account, extra_params=None):
    """One NewsData.io 'latest' request -> normalized items. Returns [] on any issue.
    NOTE: no 'language' filter — we collect ALL languages and translate later."""
    if not api_key:
        log(f"{source_label}: API key not set — skipping", "·")
        return []
    if not query:
        log(f"{source_label}: no news_query — skipping", "·")
        return []
    params = {"apikey": api_key, "q": query}
    if extra_params:
        params.update(extra_params)
    try:
        resp = requests.get("https://newsdata.io/api/1/latest", params=params, timeout=25)
        data = resp.json()
        if data.get("status") != "success":
            msg = data.get("results") or data.get("message") or data
            log(f"{source_label}: error ({msg})", "✗")
            return []
        items = []
        for a in (data.get("results") or [])[:15]:
            items.append({
                "account": account,
                "source": f"{source_label} ({a.get('source_id', 'web')})",
                "title": a.get("title") or "(no title)",
                "link": a.get("link", ""),
                "body": (a.get("description") or a.get("content") or "")[:600],
                "also": [],
            })
        log(f"{source_label}: {len(items)} article(s) for '{query}'")
        return items
    except Exception as ex:
        log(f"{source_label}: error ({ex})", "✗")
        return []

def collect_newsapi_broad(account, query):
    """Broad third-party news across all outlets — uses the 'alert' key."""
    return _newsdata_query(
        os.getenv("NEWSDATA_API_KEY_ALERT", ""),
        query, "NewsData broad", account)

def collect_newsapi_site(account, query, domain):
    """The company's OWN announcements — scoped to its domain, uses the 'website' key."""
    if not domain:
        log("NewsData site: no company_domain — skipping", "·")
        return []
    return _newsdata_query(
        os.getenv("NEWSDATA_API_KEY_WEBSITE", ""),
        query, "NewsData site", account, extra_params={"domainurl": domain})

# LinkedIn is commented out — to be enabled later with Proxycurl/Apify
# def collect_linkedin(account, company):
#     ...

def collect(account_cfg):
    name = account_cfg["name"]
    s = account_cfg.get("sources", {})
    query = s.get("news_query", name)
    log(f"collecting for {name} …")
    items = []
    items += collect_rss(s.get("website_rss", ""), "Company website", name)
    items += collect_rss(s.get("alert_rss", ""), "News alert", name)
    items += collect_newsapi_broad(name, query)
    items += collect_newsapi_site(name, query, s.get("company_domain", ""))
    # items += collect_linkedin(name, s.get("linkedin_company", ""))
    return items

# ---------------------------------------------------------------- AI brain
SYS_PROMPT = """
You are the relevance-and-scoring engine for AIONOS's B2B account-news bot,
used by account managers who sell to large enterprises.

ABOUT AIONOS (what we sell — score every item against this):
AIONOS builds agentic AI systems for enterprises in travel, aviation/transport,
hospitality, logistics, telecom, and healthcare. Core offerings:
- Customer-experience automation: voice AI, surveys, workflow automation,
  AI-assisted support (e.g. passenger/customer support).
- Operational efficiency & workflow orchestration: automating decisions and
  processes to cut cost.
- Disruption management / business-continuity agents: keeping operations
  running through disruptions.
- Revenue optimization & hyper-personalized customer engagement.
- Data intelligence and compliance-grade data governance.
We win when an account is investing in growth, modernization, customer
experience, cost reduction, resilience, or digital transformation.

YOUR JOB:
Given ONE news item about a tracked account, decide whether it is a real BUYING
SIGNAL for AIONOS and, if so, how strong. Be strict. Most news is not actionable.
A single routine operational incident (one delay, one weather event, one mishap
where nothing changes) is NOT a signal — mark it not relevant. Only flag items
that give an account manager a concrete reason to start or advance a conversation.

HARD RULES:
- If the item is NOT specifically about the tracked account itself, set
  relevant = false (other companies, generic round-ups / "top N" lists, stock
  tips, fan / flight-simulator / hobby videos).
- The source may be in ANY language (English, French, Hindi, etc.). ALWAYS write
  EVERY output field — including "title_en" — in clear, natural English.
  Translate the headline and all details. Never output non-English text.

REAL SIGNALS (relevant = true):
- Leadership change (new CEO/CIO/CTO/CFO/COO/Head of Digital) -> new agenda & budget.
- Funding, strong earnings, major capex or investment plans -> money to spend.
- Product/service launch, new routes/markets/facilities, expansion, big hiring -> scaling.
- Partnership, M&A, or a stated digital-transformation / AI / automation initiative -> direct fit.
- A PATTERN of operational pain (repeated disruptions, complaints, capacity strain) -> resilience/CX pitch.
- Regulatory/compliance change forcing them to act -> governance/compliance angle.

NOISE (relevant = false):
- One-off incidents with no lasting change ("flight struck by lightning, all safe").
- Awards/rankings/lists with no action attached.
- Social posts, anniversaries, generic PR, opinion pieces, hobby videos.

PRIORITY:
- HIGH: clear, time-sensitive signal with obvious AIONOS fit.
- MEDIUM: real signal worth a touch, not urgent.
- LOW: weak / context-only; usually set relevant = false instead.

TEAM this matters to (pick exactly ONE): Finance | Legal/Compliance | Operations | Technology/IT | Customer Experience | Executive.

Reply with ONLY a JSON object, no markdown:
{
  "relevant": true|false,
  "priority": "HIGH|MEDIUM|LOW",
  "category": "Leadership|Funding|Product|Partnership|Expansion|Operational|Regulatory|Industry|Other",
  "team": "Finance|Legal/Compliance|Operations|Technology/IT|Customer Experience|Executive",
  "title_en": "<the article headline, in English>",
  "reason": "<one short line: why it is or isn't a signal>",
  "what_happened": "<one sentence, in English>",
  "business_impact": "<why it matters for AIONOS's chance to sell, in English>",
  "recommended_action": "<concrete next step, or 'no action'>",
  "opportunity": "<which AIONOS offering fits, or 'none identified'>"
}
""".strip()

def _parse_json(text):
    text = (text or "").replace("```json", "").replace("```", "").strip()
    return json.loads(text)

def brain(item, industry):
    provider = os.getenv("AI_PROVIDER", "none")
    user = (f"Account: {item['account']}\nIndustry: {industry}\n"
            f"Source: {item['source']}\nHeadline: {item['title']}\nBody: {item['body']}")
    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            msg = client.messages.create(
                model=os.environ["ANTHROPIC_MODEL"], max_tokens=500,
                system=SYS_PROMPT, messages=[{"role": "user", "content": user}])
            return {**_parse_json(msg.content[0].text), "_by": "AI"}

        if provider == "azure":
            from openai import AzureOpenAI
            client = AzureOpenAI(
                api_key=os.environ["AZURE_OPENAI_API_KEY"],
                api_version=os.environ["AZURE_OPENAI_API_VERSION"],
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"])
            resp = client.chat.completions.create(
                model=os.environ["AZURE_OPENAI_DEPLOYMENT"], max_tokens=500,
                messages=[{"role": "system", "content": SYS_PROMPT},
                          {"role": "user", "content": user}])
            return {**_parse_json(resp.choices[0].message.content), "_by": "AI"}

        if provider == "azure_agent":
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import OpenAI

            base = os.environ["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/") + "/openai/v1"
            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://ai.azure.com/.default")
            client = OpenAI(base_url=base, api_key=token_provider)

            resp = client.responses.create(
                extra_body={"agent_reference": {
                    "type": "agent_reference",
                    "name": os.environ["AZURE_AI_AGENT_NAME"],
                    "version": os.getenv("AZURE_AI_AGENT_VERSION", "4"),
                }},
                input=[{"role": "user", "content": user}],
            )
            return {**_parse_json(resp.output_text), "_by": "AI(agent)"}

    except Exception as ex:
        if provider in ("anthropic", "azure", "azure_agent"):
            raise RuntimeError(
                f"{provider} scoring failed for '{item['title']}': {ex}") from ex
        log(f"AI brain error, using fallback ({ex})", "!")

    # ---- Keyword fallback — ONLY reached when AI_PROVIDER=none ----
    noise = ("picnic", "photos", "meme", "birthday", "anniversary", "webinar reminder")
    relevant = not any(w in item["title"].lower() for w in noise)
    return {
        "relevant": relevant,
        "priority": "MEDIUM" if relevant else "LOW",
        "category": "Other",
        "team": "Operations",
        "title_en": item["title"],
        "reason": "business signal" if relevant else "non-business / social post",
        "what_happened": item["title"],
        "business_impact": "Heuristic fallback — no AI scoring available.",
        "recommended_action": "Review manually.",
        "opportunity": "none identified",
        "_by": "fallback",
    }

# ---------------------------------------------------------------- delivery (plain text)
BAR = "=" * 48

def _title_of(item, v):
    """Always prefer the agent's English headline; fall back to the source title."""
    return v.get("title_en") or item.get("title", "")

def _render_block(item, v):
    lines = [
        BAR,
        f"ACCOUNT: {item['account'].upper()}",
        f"PRIORITY: {v.get('priority', '—')}",
        f"CATEGORY: {v.get('category', '—')}",
        f"TEAM: {v.get('team', '—')}",
        "", "HEADLINE", _title_of(item, v),
        "", "WHAT HAPPENED", v.get("what_happened", ""),
        "", "BUSINESS IMPACT", v.get("business_impact", ""),
        "", "RECOMMENDED ACTION", v.get("recommended_action", ""),
        "", "OPPORTUNITY", v.get("opportunity", "none identified"),
        "", "SOURCE", item.get("link") or "(n/a)",
    ]
    also = item.get("also", [])
    if also:
        lines.append(f"ALSO REPORTED BY: {len(also)} other source(s)")
        for a in also:
            lines.append(f"  - {a['source']}: {a['link'] or '(n/a)'}")
    lines.append(BAR)
    return "\n".join(lines)

def render_email(to_owners, subject, rows):
    head = []
    if to_owners:
        head.append(f"To: {', '.join(o['name'] for o in to_owners)} "
                    f"<{', '.join(o['email'] for o in to_owners)}>")
    head.append(f"Subject: {subject}")
    head.append("")
    blocks = [_render_block(r["item"], r["verdict"]) for r in rows]
    return "\n".join(head) + "\n" + "\n\n".join(blocks) + "\n"

# ---------------------------------------------------------------- delivery (HTML)
def _priority_color(p):
    return {"HIGH": "#c0392b", "MEDIUM": "#b9770e", "LOW": "#7f8c8d"}.get(str(p).upper(), "#7f8c8d")

def _esc(s):
    return html.escape(str(s or ""))

def render_email_html(account, rows):
    cards = ""
    for r in rows:
        it, v = r["item"], r["verdict"]
        color = _priority_color(v.get("priority"))
        title = _title_of(it, v)
        also = it.get("also", [])
        also_html = ""
        if also:
            links = "".join(
                f'<div style="font-size:12px;color:#6b7280;margin-top:3px;">• {_esc(a["source"])}: '
                f'<a href="{_esc(a["link"])}" style="color:#2563eb;">source</a></div>'
                for a in also)
            also_html = (f'<div style="margin-top:10px;"><span style="font-size:12px;'
                         f'color:#6b7280;font-weight:bold;">Also reported by {len(also)} other source(s)</span>{links}</div>')
        cards += f"""
        <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px 18px;margin-bottom:16px;">
          <div style="margin-bottom:10px;">
            <span style="background:{color};color:#ffffff;font-size:11px;font-weight:bold;padding:3px 9px;border-radius:4px;letter-spacing:.5px;">{_esc(v.get('priority','—'))}</span>
            <span style="color:#6b7280;font-size:12px;margin-left:8px;">{_esc(v.get('category','—'))} &middot; for {_esc(v.get('team','—'))}</span>
          </div>
          <div style="font-size:16px;font-weight:bold;color:#111827;margin-bottom:12px;line-height:1.35;">{_esc(title)}</div>
          <div style="font-size:13px;color:#374151;margin-bottom:7px;"><b>What happened:</b> {_esc(v.get('what_happened',''))}</div>
          <div style="font-size:13px;color:#374151;margin-bottom:7px;"><b>Why it matters:</b> {_esc(v.get('business_impact',''))}</div>
          <div style="font-size:13px;color:#374151;margin-bottom:7px;"><b>Recommended action:</b> {_esc(v.get('recommended_action',''))}</div>
          <div style="font-size:13px;color:#374151;margin-bottom:10px;"><b>Opportunity:</b> {_esc(v.get('opportunity','none identified'))}</div>
          <div style="font-size:13px;"><a href="{_esc(it.get('link',''))}" style="color:#2563eb;text-decoration:none;font-weight:bold;">Read the source &rarr;</a></div>
          {also_html}
        </div>"""
    return f"""<div style="max-width:660px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;color:#111827;padding:8px;">
      <p style="font-size:14px;margin:0 0 6px;">Hi,</p>
      <p style="font-size:14px;margin:0 0 18px;">Here is the latest account intelligence for <b>{_esc(account)}</b> &mdash; {len(rows)} update(s) worth your attention.</p>
      {cards}
      <p style="font-size:11px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:12px;margin-top:4px;">Generated automatically by the AIONOS Account News Bot. Reply to this email to flag anything that looks off.</p>
    </div>"""

# ---------------------------------------------------------------- send_email
# NOTE: accepts optional html= parameter so the UI can pass in edited HTML
def send_email(to_owners, subject, account, rows, dry_run, html=None):
    plain = render_email(to_owners, subject, rows)
    host = os.getenv("SMTP_HOST", "")
    if dry_run or not host:
        print("\n----- EMAIL (preview) -----")
        print(plain)
        print("---------------------------\n")
        return
    msg = EmailMessage()
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = ", ".join(o["email"] for o in to_owners)
    msg["Subject"] = subject
    msg.set_content(plain)
    msg.add_alternative(
        html if html is not None else render_email_html(account, rows),
        subtype="html"
    )
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(msg)
    log(f"email sent to {', '.join(o['name'] for o in to_owners)}", "✓")

def save_pending(account, subject, rows):
    os.makedirs(PENDING_DIR, exist_ok=True)
    fn = os.path.join(PENDING_DIR, f"{account}_{datetime.now():%Y%m%d_%H%M%S}.txt")
    open(fn, "w", encoding="utf-8").write(render_email([], subject, rows))
    log(f"held for review -> {os.path.relpath(fn, HERE)}", "⏸")

# ---------------------------------------------------------------- pipeline
def run_once(config, dry_run):
    provider = os.getenv("AI_PROVIDER", "none")
    print(f"\n[AI provider: {provider}]  [{'DRY-RUN' if dry_run else 'LIVE SEND'}]")

    defaults = config.get("defaults", {})
    review_mode = defaults.get("review_mode", False)
    sent = load_sent()
    digest_buffer = {}

    for acct in config["accounts"]:
        name = acct["name"]
        mode = acct.get("mode", defaults.get("mode", "instant"))
        owners = acct["owners"]
        print(f"\n=== {name} ({mode}) ===")

        for cluster in cluster_items(collect(acct)):
            item = cluster[0]
            item["also"] = [{"source": x["source"], "link": x["link"]} for x in cluster[1:]]
            if len(cluster) > 1:
                log(f"merged {len(cluster)} sources: {item['title']}", "↻")

            k = item_key(name, item["title"])
            if k in sent:
                log(f"duplicate, skip: {item['title']}", "·"); continue

            v = brain(item, acct.get("industry", ""))
            if not v.get("relevant"):
                log(f"skip (not relevant, {v['_by']}): {item['title']}", "✗"); continue

            row = {"item": item, "verdict": v}
            headline = _title_of(item, v)
            log(f"relevant · {v.get('priority')} {v.get('category')} ({v['_by']}): {headline}", "✓")

            subject = f"[{name}] {v.get('priority')} · {v.get('category')}: {headline}"
            if review_mode:
                save_pending(name, subject, [row]); continue
            if mode == "digest":
                digest_buffer.setdefault(name, []).append(row); continue
            send_email(owners, subject, name, [row], dry_run)
            sent.add(k)

    for acct in config["accounts"]:
        rows = digest_buffer.get(acct["name"])
        if rows:
            subj = f"[{acct['name']}] Daily digest — {len(rows)} update(s)"
            send_email(acct["owners"], subj, acct["name"], rows, dry_run)
            for r in rows:
                sent.add(item_key(acct["name"], r["item"]["title"]))

    save_sent(sent)
    print("\n— run complete —")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="print emails instead of sending")
    args = ap.parse_args()
    config = yaml.safe_load(open(args.config))
    run_once(config, args.dry_run)

if __name__ == "__main__":
    main()