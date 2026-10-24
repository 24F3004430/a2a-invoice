import json
from typing import Any
BATCH_MEDIA="application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_MEDIA="application/vnd.ga5.invoice-action-proposals+json"
RESULTS_MEDIA="application/vnd.ga5.invoice-action-results+json"
RECEIPT_MEDIA="application/vnd.ga5.invoice-action-receipts+json"
ACTIONS={"settle_invoice","request_approval","hold_invoice","reject_duplicate","open_exception"}

def canonical_json(v:Any)->str:
    return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False)

def validate_batch(d):
    if not isinstance(d,dict) or not isinstance(d.get("batchId"),str) or not d["batchId"]:
        raise ValueError("Missing batchId")
    if not isinstance(d.get("policyRevision"),str) or not d["policyRevision"]:
        raise ValueError("Missing policyRevision")
    ps=d.get("packages")
    if not isinstance(ps,list) or not ps: raise ValueError("packages must be nonempty")
    ids=[p.get("packageId") for p in ps if isinstance(p,dict)]
    if len(ids)!=len(ps) or len(set(ids))!=len(ids) or any(not isinstance(x,str) or not x for x in ids):
        raise ValueError("Every package needs a unique packageId")
    return d

def validate_proposals(batch, proposals):
    expected={p["packageId"] for p in batch["packages"]}
    if not isinstance(proposals,list) or len(proposals)!=len(expected): raise ValueError("One proposal per package required")
    seenp,seena=set(),set(); out=[]
    for p in proposals:
        if not isinstance(p,dict): raise ValueError("Proposal must be an object")
        pid=p.get("packageId"); aid=p.get("actionId")
        if pid not in expected or pid in seenp: raise ValueError("Unknown/duplicate packageId")
        seenp.add(pid)
        if not isinstance(aid,str) or len(aid)<12 or aid in seena: raise ValueError("actionId must be unique and >=12 chars")
        seena.add(aid)
        if p.get("action") not in ACTIONS: raise ValueError("Invalid action")
        f=p.get("facts")
        if not isinstance(f,dict) or not all(k in f for k in ("vendorName","invoiceNumber","amountMinor","currency")): raise ValueError("Missing facts")
        if not isinstance(f["amountMinor"],int) or isinstance(f["amountMinor"],bool): raise ValueError("amountMinor must be integer")
        refs=p.get("evidenceRefs")
        if not isinstance(refs,list) or len(refs)!=3 or any(not isinstance(x,str) or not x for x in refs): raise ValueError("Exactly three evidence refs required")
        r=p.get("rationale")
        if not isinstance(r,str) or not 60<=len(r)<=1500: raise ValueError("Rationale must be 60-1500 chars")
        if p["action"] not in r or sum(ref in r for ref in refs)<2: raise ValueError("Rationale must name action and cite at least two refs")
        out.append({k:p[k] for k in ("packageId","actionId","action","facts","evidenceRefs","rationale")})
    if seenp!=expected: raise ValueError("Missing package proposal")
    return out

def validate_results(d):
    if not isinstance(d,dict) or not isinstance(d.get("batchId"),str): raise ValueError("Invalid results")
    rs=d.get("results")
    if not isinstance(rs,list): raise ValueError("results must be array")
    seen=set()
    for r in rs:
        if not isinstance(r,dict): raise ValueError("Invalid result")
        for k in ("packageId","actionId","action","outcome","receiptNonce"):
            if not isinstance(r.get(k),str) or not r[k]: raise ValueError(f"Missing {k}")
        if r["action"] not in ACTIONS or r["outcome"] not in ("ACCEPTED","REJECTED"): raise ValueError("Invalid result values")
        if r["packageId"] in seen: raise ValueError("Duplicate package result")
        seen.add(r["packageId"])
    return d["batchId"],rs
