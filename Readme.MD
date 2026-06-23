# Account News Bot — starter

A small, config-driven bot that watches each account's news (company website,
Google Alerts, and LinkedIn) and emails the owning account manager. It runs
**today** with no keys (skips empty feeds, uses a fallback brain, prints emails),
and you switch on each real piece one at a time.

---

## The order of work (do these top to bottom)

1. **Set up the project** — install Python deps, copy `.env.example` to `.env`.
2. **Get the free feeds** — create a Google Alert (RSS) per account, find each
   company website's RSS. Paste both URLs into `config.yaml`. *(free)*
3. **Create the Azure AI agent** — make an agent in the Foundry portal, run
   `az login`, and put `PROJECT_ENDPOINT` + `AGENT_NAME` in `.env`. *(no API key)*
4. **Decide LinkedIn** — sign up with a third-party data provider (no approval),
   or apply to LinkedIn officially (slow). Add the key to `.env` when ready. *(paid)*
5. **Run it** with `--dry-run` and read the email previews.
6. **Turn on email** — add SMTP details, drop `--dry-run`.
7. **Move to Azure** — run it on a schedule with Azure Functions; swap the local
   `sent.json` for Azure Table Storage and SMTP for Azure Communication Services.

---

## 1. Set up

```bash
pip install -r requirements.txt
cp .env.example .env
```

## 2. Get the free feeds

**Google Alerts → RSS (per account):**
1. Go to google.com/alerts and sign in.
2. Type the query, e.g. `AIONOS` (add industry terms if you like).
3. Click **Show options** → set Region to *Any Region* (for global), Sources to
   *Automatic*, How many to *All results*.
4. In **Deliver to**, choose **RSS feed** → **Create Alert**.
5. On the alerts list, click the **RSS icon** next to your alert and copy that URL.
6. Paste it into `alert_rss` for that account in `config.yaml`.

**Company website RSS (per account):**
1. Open the company's news / press / blog page.
2. Look for an RSS icon, or try `…/feed`, `…/rss`, `…/news/feed`, `…/blog/feed`.
3. Or view page source and search for `application/rss+xml` to find the link.
4. Paste it into `website_rss`. (No RSS? Use a page-to-feed / change-detection
   service, or leave blank for now.)

## 3. Create the Azure AI agent (the brain)

The brain is an **Azure AI Foundry agent**, called through the Microsoft SDK and
authenticated with your `az login` session (no API keys in the code).

1. Install the SDK: `pip install "azure-ai-projects>=2.0.0" azure-identity`
2. In the Foundry portal (ai.azure.com), create a project and deploy a model.
3. Create an **agent** and paste these instructions (so it returns clean JSON):
   > You are the relevance-and-summary engine for a B2B account-news bot. Given one
   > news item about a tracked account, decide if it is genuinely newsworthy for a
   > sales account manager (social/non-business posts are NOT relevant). Reply with
   > ONLY a JSON object: `{"relevant": true|false, "reason": "...",
   > "category": "Leadership|Funding|Product|Partnership|Expansion|Industry|Other",
   > "summary": "one or two sentence why-it-matters"}`
4. Make sure your account has the **Foundry User** role on the project.
5. Sign in so the SDK can authenticate:
   ```bash
   az login
   ```
6. In `.env`, set `PROJECT_ENDPOINT`
   (`https://<resource>.services.ai.azure.com/api/projects/<project>`) and
   `AGENT_NAME` (the agent you just created).

> Note: the code has no try/except around the agent call, so if `az login`, the
> endpoint, or the agent name is wrong, you'll see the real error — that's intended.

## 4. LinkedIn

LinkedIn's own API will not give you another company's posts (that needs a rarely
granted partner approval). Two routes:
- **Third-party provider (recommended):** sign up (e.g. a LinkedIn data API
  provider), get an API key — **no approval needed**. Set `LINKEDIN_PROVIDER`,
  `LINKEDIN_API_KEY`, `LINKEDIN_API_BASE`, then map their response in
  `collect_linkedin()`.
- **Official LinkedIn:** apply at developer.linkedin.com (see README section in chat).

## 5–6. Run

```bash
python newsbot.py --dry-run     # prints email previews, sends nothing
python newsbot.py               # sends email if SMTP is set, else still prints
```

For email, fill the `SMTP_*` and `EMAIL_FROM` values in `.env`
(Office 365 SMTP host is `smtp.office365.com`).

## 7. Move to Azure (later)

- **Schedule:** wrap `run_once()` in an **Azure Function** with a timer trigger
  (e.g. every 30 min), or call this script from **Azure Logic Apps**.
- **Dedupe memory:** replace the `sent.json` file with **Azure Table Storage**.
- **Email:** replace SMTP with **Azure Communication Services Email** or the
  Logic Apps **Office 365 Outlook** action.
- **Config:** move `config.yaml` into a **SharePoint list / Excel** the flow reads.
- **Review gate:** set `review_mode: true` to write drafts to `./pending` for
  Anjali; in Azure this becomes a Logic Apps **Approval** step.

## Files
- `config.yaml` — accounts, owners, sources (the config layer)
- `.env` — keys & secrets (copied from `.env.example`)
- `newsbot.py` — the pipeline
- `sent.json` — auto-created dedupe memory
- `pending/` — auto-created drafts when `review_mode` is on