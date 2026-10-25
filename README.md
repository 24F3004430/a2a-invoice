# A2A Invoice Agent (GitHub + Render ready)

A web service that reads invoice batches, uses an AI model to pick one action per invoice
(`settle_invoice`, `request_approval`, `hold_invoice`, `reject_duplicate`, `open_exception`),
returns **proposals**, waits for the grader's **results**, and only then files **receipts**.

```
main.py       protocol rules (routes, auth, idempotency, cancel/result race)
storage.py    SQLite: tasks, replay records, decision cache (atomic transactions)
decisions.py  the AI call (one batched call, strict validation, one repair retry)
schemas.py    constants + validators (evidence must really exist in the documents)
tests/        test_logic.py (stdlib only) and test_api.py (full flow with a fake AI)
render.yaml   one-click deploy blueprint for Render
```

## 1. Put it on GitHub (no command line needed)

1. Create a free account at github.com, click **New repository**, name it `a2a-invoice-agent`, keep it **Private**, create it.
2. Click **uploading an existing file**, drag in everything from this folder (including hidden files such as `.gitignore`, if your computer shows them), and **Commit changes**.
3. Never upload real keys. `.env` is already in `.gitignore`.

## 2. Get a free AI key

Any provider with an OpenAI-compatible `/chat/completions` endpoint works; the provider is not graded.
You need three values: `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`. Examples (check the provider's docs for current model names):

| Provider | AI_BASE_URL |
|---|---|
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` |
| Groq | `https://api.groq.com/openai/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |

Each core batch is roughly 12k tokens of input, so pick a model with a big context window and a generous
free-tier per-minute limit. If your provider rejects `response_format`, set `AI_JSON_MODE=0`.

## 3. Deploy on Render

1. Sign in at render.com with GitHub.
2. **New + → Blueprint**, choose your repo. Render reads `render.yaml`.
3. Fill in `AI_BASE_URL`, `AI_MODEL`, `AI_API_KEY` when asked. `A2A_BEARER_TOKEN` is generated for you.
4. Wait for "Live". The disk in `render.yaml` keeps your database between restarts (this needs a paid
   instance type; check Render's current pricing). **Do not use a free instance for the graded run:** it sleeps
   (slow first request) and has no persistent disk, so retries could repeat work.
5. Open **Environment** in the dashboard and copy the generated `A2A_BEARER_TOKEN`; the grader needs it.
6. Your base URL is `https://<your-service>.onrender.com/a2a/`. The server picks this up automatically
   (`RENDER_EXTERNAL_URL`). Visit `https://<your-service>.onrender.com/.well-known/agent-card.json`:
   `supportedInterfaces[0].url` must equal the URL you submit, character for character.
   If you use a custom domain, set `A2A_BASE_URL` yourself.

Keep it at **one worker** (the start command already does). The per-message locks live in memory.

## 4. Test before you spend graded checks (you only have 10)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tests/test_logic.py     # no dependencies needed beyond Python
python tests/test_api.py       # whole protocol, fake AI, no key, no cost
```

Both must print `PASSED`. Then use **Check** a few times, read the failure codes, fix, and use the last
check for **Save** (saving with no checks left scores 0).

## 5. Optional local run

```bash
export A2A_BEARER_TOKEN='long-random-secret' A2A_BASE_URL='http://localhost:8000/a2a/'
export DATABASE_PATH=./invoice_agent.sqlite3 AI_BASE_URL=... AI_API_KEY=... AI_MODEL=...
uvicorn main:app --port 8000
```

## What changed from the starter

- ProtoJSON states `TASK_STATE_COMPLETED` / `TASK_STATE_CANCELED` (the starter used bare `COMPLETED`/`CANCELED`).
- Success responses use `application/a2a+json`; `GET /tasks/{id}` returns the bare Task; artifacts have `artifactId`.
- Concurrent identical messages: per-(user, messageId) lock + task and replay record written in one transaction, so one model call and one task.
- Result continuations must cover **every** package with matching action IDs and actions; replays return the same Task.
- Cancel-vs-result race is decided inside one `BEGIN IMMEDIATE` transaction: exactly one wins, the other gets 409.
- Evidence refs are checked against the real package text; the model's wording is repaired so rationales always name the action and cite refs.
- Decision cache keyed by package content (ids excluded). Action IDs are minted fresh per task.
- Fresh model failures return 502 and store nothing, so a retry can try again.
- Base-URL path drives the route prefix; any extra tokens in `A2A_EXTRA_TOKENS` become separate users.

## Honest limits

I could not run this against the real grader or a real AI model. The pure logic (validators, storage, race)
is tested; the HTTP layer has a full test you should run once. The prompt in `decisions.py` is general:
if Check feedback shows decision-quality failures, tune it using the failure codes (never paste secrets or real
finance data into prompts).
