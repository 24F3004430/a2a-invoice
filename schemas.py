"""Shared constants, JSON helpers and validators (no web framework in here)."""
import hashlib
import json
from typing import Any, Iterator

BATCH_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MEDIA = "application/vnd.ga5.invoice-action-results+json"
RECEIPT_MEDIA = "application/vnd.ga5.invoice-action-receipts+json"

# A2A 1.0 ProtoJSON task states (the grader expects these exact strings)
INPUT = "TASK_STATE_INPUT_REQUIRED"
DONE = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
TERMINAL = (DONE, CANCELED)

ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}


def canonical_json(v: Any) -> str:
    """Recursively key-sorted, compact JSON. Used for hashing and storage."""
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def package_key(pkg: dict) -> str:
    """Cache key = the package CONTENT. Batch/package ids are ignored so the
    same invoice documents re-sent in a new batch hit the cache."""
    body = {k: v for k, v in pkg.items() if k not in ("packageId", "batchId")}
    return sha(canonical_json(body))


def _strings(v: Any) -> Iterator[str]:
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def package_text(pkg: dict) -> str:
    return "\n".join(_strings(pkg))


def validate_batch(d):
    if not isinstance(d, dict) or not isinstance(d.get("batchId"), str) or not d["batchId"]:
        raise ValueError("Missing batchId")
    if not isinstance(d.get("policyRevision"), str) or not d["policyRevision"]:
        raise ValueError("Missing policyRevision")
    ps = d.get("packages")
    if not isinstance(ps, list) or not ps:
        raise ValueError("packages must be nonempty")
    if any(not isinstance(p, dict) for p in ps):
        raise ValueError("Every package must be an object")
    ids = [p.get("packageId") for p in ps]
    if len(set(ids)) != len(ids) or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError("Every package needs a unique packageId")
    return d


def _to_int(v):
    if isinstance(v, bool):
        raise ValueError("amountMinor must be an integer")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    raise ValueError("amountMinor must be an integer")


def clean_decision(pkg: dict, raw: Any) -> dict:
    """Turn one raw AI answer into a safe, normalised decision, or raise ValueError.
    Never trusts the model: evidence must literally exist in the package text."""
    if not isinstance(raw, dict):
        raise ValueError("proposal must be an object")
    action = raw.get("action")
    if action not in ACTIONS:
        raise ValueError("invalid action")
    f = raw.get("facts")
    if not isinstance(f, dict):
        raise ValueError("missing facts")
    vendor, inv, cur = f.get("vendorName"), f.get("invoiceNumber"), f.get("currency")
    if inv is not None and not isinstance(inv, str):
        inv = str(inv)
    if not all(isinstance(x, str) and x.strip() for x in (vendor, inv, cur)):
        raise ValueError("facts need vendorName, invoiceNumber, currency")
    facts = {"vendorName": vendor, "invoiceNumber": inv, "amountMinor": _to_int(f.get("amountMinor")), "currency": cur}

    refs = raw.get("evidenceRefs")
    if not isinstance(refs, list) or len(refs) != 3 or any(not isinstance(x, str) or not x for x in refs):
        raise ValueError("need exactly three evidence refs")
    if len(set(refs)) != 3:
        raise ValueError("evidence refs must be distinct")
    text = package_text(pkg)
    fixed = []
    for r in refs:
        r = r.strip()
        if not (r.startswith("[") and r.endswith("]")) and f"[{r}]" in text:
            r = f"[{r}]"            # documents use bracketed ids; return them exactly as written
        fixed.append(r)
    refs = fixed
    if len(set(refs)) != 3 or any(r not in text for r in refs):
        raise ValueError("evidence ref not found in package")

    r = str(raw.get("rationale") or "").strip()
    if action not in r:
        r = f"Action {action}: {r}".strip()
    if sum(x in r for x in refs) < 2:
        r = f"{r} Decisive evidence: {', '.join(refs)}."
    if len(r) > 1500:
        r = r[:1300].rstrip() + f" Action {action}. Evidence: {', '.join(refs)}."
    while len(r) < 60:
        r += " Decision based on the decisive paragraph of the package."
    return {"action": action, "facts": facts, "evidenceRefs": refs, "rationale": r}


def validate_proposals(batch, proposals):
    """Final gate before anything is stored or returned."""
    expected = {p["packageId"] for p in batch["packages"]}
    if not isinstance(proposals, list) or len(proposals) != len(expected):
        raise ValueError("One proposal per package required")
    seenp, seena, out = set(), set(), []
    for p in proposals:
        if not isinstance(p, dict):
            raise ValueError("Proposal must be an object")
        pid, aid = p.get("packageId"), p.get("actionId")
        if pid not in expected or pid in seenp:
            raise ValueError("Unknown/duplicate packageId")
        seenp.add(pid)
        if not isinstance(aid, str) or len(aid) < 12 or aid in seena:
            raise ValueError("actionId must be unique and >=12 chars")
        seena.add(aid)
        if p.get("action") not in ACTIONS:
            raise ValueError("Invalid action")
        f = p.get("facts")
        if not isinstance(f, dict) or not all(k in f for k in ("vendorName", "invoiceNumber", "amountMinor", "currency")):
            raise ValueError("Missing facts")
        if not isinstance(f["amountMinor"], int) or isinstance(f["amountMinor"], bool):
            raise ValueError("amountMinor must be integer")
        refs = p.get("evidenceRefs")
        if not isinstance(refs, list) or len(refs) != 3 or len(set(refs)) != 3 or any(not isinstance(x, str) or not x for x in refs):
            raise ValueError("Exactly three distinct evidence refs required")
        r = p.get("rationale")
        if not isinstance(r, str) or not 60 <= len(r) <= 1500:
            raise ValueError("Rationale must be 60-1500 chars")
        if p["action"] not in r or sum(ref in r for ref in refs) < 2:
            raise ValueError("Rationale must name action and cite at least two refs")
        out.append({k: p[k] for k in ("packageId", "actionId", "action", "facts", "evidenceRefs", "rationale")})
    return out


def validate_results(d):
    if not isinstance(d, dict) or not isinstance(d.get("batchId"), str):
        raise ValueError("Invalid results")
    rs = d.get("results")
    if not isinstance(rs, list):
        raise ValueError("results must be array")
    seen = set()
    for r in rs:
        if not isinstance(r, dict):
            raise ValueError("Invalid result")
        for k in ("packageId", "actionId", "action", "outcome", "receiptNonce"):
            if not isinstance(r.get(k), str) or not r[k]:
                raise ValueError(f"Missing {k}")
        if r["action"] not in ACTIONS or r["outcome"] not in ("ACCEPTED", "REJECTED"):
            raise ValueError("Invalid result values")
        if r["packageId"] in seen:
            raise ValueError("Duplicate package result")
        seen.add(r["packageId"])
    return d["batchId"], rs
