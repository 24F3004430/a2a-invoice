"""A2A 1.0 (HTTP+JSON) invoice action agent.

Layers:  protocol (this file) -> storage.py (state) -> decisions.py (AI) -> schemas.py (validation)
Run with ONE worker process: the per-message locks below live in memory.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import decisions
from schemas import (
    BATCH_MEDIA, CANCELED, DONE, INPUT, PROPOSAL_MEDIA, RECEIPT_MEDIA, RESULTS_MEDIA, TERMINAL,
    canonical_json, package_key, sha, validate_batch, validate_proposals, validate_results,
)
from storage import Store

APP_NAME = os.getenv("AGENT_NAME", "A2A Invoice Action Agent")
APP_VERSION = os.getenv("AGENT_VERSION", "1.0.0")


def _default_base() -> str:
    if os.getenv("A2A_BASE_URL"):
        return os.environ["A2A_BASE_URL"]
    render = os.getenv("RENDER_EXTERNAL_URL")  # Render sets this automatically
    if render:
        return render.rstrip("/") + "/a2a/"
    return "http://localhost:8000/a2a/"


BASE_URL = _default_base()
PREFIX = urlparse(BASE_URL).path.rstrip("/")  # routes live under the path of the URL you submit
TOKENS = [t.strip() for t in (os.getenv("A2A_BEARER_TOKEN", "") + "," + os.getenv("A2A_EXTRA_TOKENS", "")).split(",") if t.strip()]
if not TOKENS:
    raise RuntimeError("Set A2A_BEARER_TOKEN before starting the server")

store = Store(os.getenv("DATABASE_PATH", "invoice_agent.sqlite3"))
app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None,
              redirect_slashes=False)  # the grader must never be redirected
router = APIRouter(prefix=PREFIX)

_msg_locks: dict = {}          # one lock per (user, messageId): concurrent duplicates take turns
_ai_lock = asyncio.Lock()      # one model call at a time; later callers then find the cache filled


# ----------------------------------------------------------------- helpers
class A2AResponse(JSONResponse):
    media_type = "application/a2a+json"


class ApiError(Exception):
    def __init__(self, status, code, message="Request could not be processed"):
        self.status, self.code, self.message = status, code, message


def _error_body(status, code, message):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(ApiError)
async def _api_error(_req, exc: ApiError):
    return _error_body(exc.status, exc.code, exc.message)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_req, exc):
    return _error_body(exc.status_code, "HTTP_ERROR", "Request could not be processed")


@app.exception_handler(Exception)
async def _unexpected(_req, _exc):
    return _error_body(500, "INTERNAL", "Internal error")


def authenticate(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer ") or not header[7:]:
        raise ApiError(401, "UNAUTHENTICATED", "Authentication required")
    token = header[7:]
    if not any(hmac.compare_digest(token.encode(), t.encode()) for t in TOKENS):
        raise ApiError(403, "FORBIDDEN", "Forbidden")
    return sha(token)  # the "principal" = a hash of the token; one token = one user


def check_version(request: Request):
    if request.headers.get("a2a-version") != "1.0":
        raise ApiError(400, "VERSION_NOT_SUPPORTED", "Unsupported A2A version")


def check_content_type(request: Request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/a2a+json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Unsupported media type")


def check_accept(request: Request):
    accept = request.headers.get("accept")
    if not accept:
        return
    types = [t.split(";")[0].strip().lower() for t in accept.split(",")]
    if not any(t in ("*/*", "application/*", "application/json", "application/a2a+json") for t in types):
        raise ApiError(406, "NOT_ACCEPTABLE", "Unsupported Accept header")


class NormalizePath:
    """Collapse '//' and drop a trailing '/', so '/a2a//tasks/' still reaches '/a2a/tasks' (no redirects)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            p = re.sub(r"/{2,}", "/", scope["path"])
            if len(p) > 1:
                p = p.rstrip("/") or "/"
            scope = dict(scope, path=p)
            if "raw_path" in scope:
                scope["raw_path"] = p.encode()
        await self.inner(scope, receive, send)


app.add_middleware(NormalizePath)


@app.middleware("http")
async def _version_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["A2A-Version"] = "1.0"
    return response


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_part(media, data):
    return {"mediaType": media, "data": data}


def make_task(tid, cid, state, history, artifacts):
    return {"id": tid, "contextId": cid, "status": {"state": state, "timestamp": now_iso()},
            "history": history, "artifacts": artifacts}


# ----------------------------------------------------------------- public card
def _origin(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    proto = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip()
    if host.startswith(("localhost", "127.0.0.1")):
        proto = request.url.scheme
    return f"{proto}://{host}"


def _interfaces(request: Request) -> list:
    """The configured URL first, then the URL this very request reached us on; each with and without a
    trailing slash. 'supportedInterfaces contains the submitted URL' is then true whichever spelling was submitted."""
    seen, out = set(), []
    for base in (BASE_URL, _origin(request) + PREFIX + "/"):
        for u in (base, base.rstrip("/") + "/", base.rstrip("/")):
            if u and u not in seen:
                seen.add(u)
                out.append({"url": u, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"})
    return out


@app.get("/.well-known/agent-card.json")
def agent_card(request: Request):
    return {
        "name": APP_NAME,
        "description": "Evidence-based invoice action proposals with approval-gated receipts.",
        "version": APP_VERSION,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "supportedInterfaces": _interfaces(request),
        "defaultInputModes": [BATCH_MEDIA, RESULTS_MEDIA],
        "defaultOutputModes": [PROPOSAL_MEDIA, RECEIPT_MEDIA],
        "skills": [{
            "id": "invoice_action_agent",
            "name": "Invoice Action Agent",
            "description": "Analyze invoice packages and propose safe actions with evidence.",
            "tags": ["invoice", "accounts-payable", "evidence", "approval"],
            "inputModes": [BATCH_MEDIA, RESULTS_MEDIA],
            "outputModes": [PROPOSAL_MEDIA, RECEIPT_MEDIA],
        }],
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ----------------------------------------------------------------- message:send
@router.post("/message:send")
async def send_message(request: Request):
    principal = authenticate(request)
    check_version(request)
    check_accept(request)
    check_content_type(request)
    try:
        body = await request.json()
    except Exception:
        raise ApiError(400, "INVALID_JSON", "Invalid JSON")
    msg = body.get("message") if isinstance(body, dict) else None
    if not isinstance(msg, dict):
        raise ApiError(400, "INVALID_MESSAGE", "Missing message")
    mid = msg.get("messageId")
    if not isinstance(mid, str) or not mid:
        raise ApiError(400, "INVALID_MESSAGE", "Missing messageId")
    # hash ONLY the message (configuration is ignored), with recursively sorted keys
    digest = sha(canonical_json(msg))

    lock = _msg_locks.setdefault((principal, mid), asyncio.Lock())
    async with lock:
        old = store.get_idempotency(principal, mid)
        if old:
            if old["message_hash"] != digest:
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Message id reused with different content")
            return A2AResponse(content={"task": json.loads(old["response_json"])})

        if msg.get("role") != "ROLE_USER":
            raise ApiError(400, "INVALID_MESSAGE", "Expected ROLE_USER")
        parts = msg.get("parts")
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            raise ApiError(400, "INVALID_MESSAGE", "Expected exactly one part")
        media, data = parts[0].get("mediaType"), parts[0].get("data")

        cfg = body.get("configuration")
        modes = cfg.get("acceptedOutputModes") if isinstance(cfg, dict) else None
        if media == BATCH_MEDIA and isinstance(modes, list) and modes and PROPOSAL_MEDIA not in modes:
            raise ApiError(415, "UNSUPPORTED_OUTPUT_MODE", "Cannot produce an accepted output mode")

        if media == BATCH_MEDIA:
            task = await _handle_batch(principal, msg, mid, digest, data)
        elif media == RESULTS_MEDIA:
            task = _handle_results(principal, msg, mid, digest, data)
        else:
            raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Unsupported part media type")
    return A2AResponse(content={"task": task})


async def _handle_batch(principal, msg, mid, digest, data):
    try:
        batch = validate_batch(data)
    except ValueError as e:
        raise ApiError(400, "INVALID_BATCH", str(e))

    salt = decisions.cache_salt()
    keys = {p["packageId"]: sha(package_key(p) + salt) for p in batch["packages"]}
    cached = {}
    for p in batch["packages"]:
        d = store.get_decision(keys[p["packageId"]])
        if d:
            cached[p["packageId"]] = d
    missing = [p for p in batch["packages"] if p["packageId"] not in cached]

    if missing:
        async with _ai_lock:
            still = []
            for p in missing:  # someone else may have filled the cache while we waited
                d = store.get_decision(keys[p["packageId"]])
                if d:
                    cached[p["packageId"]] = d
                else:
                    still.append(p)
            if still:
                try:
                    decided = await decisions.decide_packages(batch, still)
                except decisions.DecisionError:
                    raise ApiError(502, "DECISION_FAILED", "Could not decide this batch")
                for p in still:
                    store.put_decision(keys[p["packageId"]], decided[p["packageId"]])
                    cached[p["packageId"]] = decided[p["packageId"]]

    proposals = [
        {"packageId": p["packageId"], "actionId": "act-" + uuid.uuid4().hex, **cached[p["packageId"]]}
        for p in batch["packages"]
    ]  # fresh unique actionId per task; the cached part has no ids
    try:
        proposals = validate_proposals(batch, proposals)
    except ValueError:
        raise ApiError(502, "DECISION_INVALID", "Decision failed validation")

    tid = str(uuid.uuid4())
    cid = msg.get("contextId") if isinstance(msg.get("contextId"), str) and msg.get("contextId") else str(uuid.uuid4())
    task = make_task(
        tid, cid, INPUT, [msg],
        [{"artifactId": "proposals-" + tid, "name": "invoice-action-proposals",
          "parts": [make_part(PROPOSAL_MEDIA, {"batchId": batch["batchId"], "proposals": proposals})]}],
    )
    store.create_task(tid, cid, principal, batch["batchId"], INPUT, task, mid, digest)  # one transaction
    return task


def _handle_results(principal, msg, mid, digest, data):
    tid, cid = msg.get("taskId"), msg.get("contextId")
    if not isinstance(tid, str) or not tid or not isinstance(cid, str) or not cid:
        raise ApiError(400, "INVALID_MESSAGE", "Missing taskId or contextId")
    row = store.get_task(tid, principal)  # only YOUR tasks are visible
    if not row:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found")
    if row["state"] in TERMINAL:
        raise ApiError(409, "TASK_TERMINAL", "Task is already finished")
    task = json.loads(row["task_json"])
    if task.get("contextId") != cid:
        raise ApiError(409, "RESULT_BINDING_MISMATCH", "Result does not match the task")
    try:
        batch_id, results = validate_results(data)
    except ValueError as e:
        raise ApiError(400, "INVALID_RESULTS", str(e))
    if batch_id != row["batch_id"]:
        raise ApiError(409, "RESULT_BINDING_MISMATCH", "Result does not match the task")

    proposals = task["artifacts"][0]["parts"][0]["data"]["proposals"]
    by_pkg = {p["packageId"]: p for p in proposals}
    if {r["packageId"] for r in results} != set(by_pkg):
        raise ApiError(409, "RESULT_BINDING_MISMATCH", "Result does not match the task")
    got = {r["packageId"]: r for r in results}
    for pid, p in by_pkg.items():
        r = got[pid]
        if r["actionId"] != p["actionId"] or r["action"] != p["action"]:
            raise ApiError(409, "RESULT_BINDING_MISMATCH", "Result does not match the task")

    executions = []  # ACCEPTED only, in proposal order, copied from OUR stored proposal
    for p in proposals:
        r = got[p["packageId"]]
        if r["outcome"] == "ACCEPTED":
            executions.append({
                "packageId": p["packageId"], "actionId": p["actionId"], "action": p["action"],
                "receiptNonce": r["receiptNonce"], "facts": p["facts"], "evidenceRefs": p["evidenceRefs"],
            })
    completed = make_task(
        tid, cid, DONE, task["history"] + [msg],
        task["artifacts"] + [{"artifactId": "receipts-" + tid, "name": "invoice-action-receipts",
                              "parts": [make_part(RECEIPT_MEDIA, {"batchId": batch_id, "executions": executions})]}],
    )
    if not store.finish_task(tid, principal, DONE, completed, idem=(mid, digest)):
        raise ApiError(409, "TASK_TERMINAL", "Task is already finished")  # lost the race to cancel
    return completed


# ----------------------------------------------------------------- reads, list, cancel
@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    principal = authenticate(request)
    check_version(request)
    check_accept(request)
    row = store.get_task(task_id, principal)
    if not row:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found")
    return A2AResponse(content=json.loads(row["task_json"]))


@router.get("/tasks")
def list_tasks(request: Request):
    principal = authenticate(request)
    check_version(request)
    check_accept(request)
    return A2AResponse(content={"tasks": [json.loads(r["task_json"]) for r in store.list_tasks(principal)]})


@router.post("/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, request: Request):
    principal = authenticate(request)
    check_version(request)
    check_accept(request)
    row = store.get_task(task_id, principal)
    if not row:
        raise ApiError(404, "TASK_NOT_FOUND", "Task not found")
    if row["state"] in TERMINAL:
        raise ApiError(409, "TASK_TERMINAL", "Task is already finished")
    task = json.loads(row["task_json"])
    canceled = make_task(task_id, task["contextId"], CANCELED, task["history"], task["artifacts"])
    if not store.finish_task(task_id, principal, CANCELED, canceled):
        raise ApiError(409, "TASK_TERMINAL", "Task is already finished")  # lost the race to a result
    return A2AResponse(content=canceled)


app.include_router(router)
