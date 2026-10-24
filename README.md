# A2A Invoice Agent starter

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Export the values in `.env` into your shell (or configure them in your host dashboard). A `.env` loader is not included.

```bash
export A2A_BEARER_TOKEN='use-a-long-random-secret'
export A2A_BASE_URL='http://localhost:8000/a2a/'
export DATABASE_PATH='./invoice_agent.sqlite3'
export AI_BASE_URL='https://api.openai.com/v1'
export AI_API_KEY='your-key'
export AI_MODEL='your-json-capable-model'
uvicorn main:app --host 0.0.0.0 --port 8000
```

The API requires `Authorization: Bearer ...`, `A2A-Version: 1.0`, and POST content type `application/a2a+json`. The public Agent Card is `/.well-known/agent-card.json`.

## Smoke tests

```bash
curl -i http://127.0.0.1:8000/.well-known/agent-card.json
curl -i http://127.0.0.1:8000/a2a/tasks \
  -H 'Authorization: Bearer use-a-long-random-secret' \
  -H 'A2A-Version: 1.0'
```

## Important limitations before graded use

This is a starter, not a verified full-score submission. Review the exact A2A 1.0 Task/Agent Card wire schema with the grader. It still needs hardening for atomic idempotency reservation before AI work, response media types, duplicate result-continuation replay, full expected-result-set validation, cancellation message contract, and rigorous concurrent testing. The simple token config treats one configured token as one principal. Use a host with durable storage; ephemeral SQLite files are not safe for this task. Validate evidence refs against source text and test fresh audit packages before spending assignment checks.
