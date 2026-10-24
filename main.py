import os, json, uuid, hashlib
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
from schemas import BATCH_MEDIA, RESULTS_MEDIA, PROPOSAL_MEDIA, RECEIPT_MEDIA, canonical_json, validate_batch, validate_proposals, validate_results
from decisions import decide_batch
from storage import Store

APP_NAME = os.getenv("AGENT_NAME", "A2A Invoice Action Agent")
APP_VERSION = os.getenv("AGENT_VERSION", "1.0.0")
BASE_URL = os.getenv("A2A_BASE_URL", "http://localhost:8000/a2a/")
TOKEN = os.getenv("A2A_BEARER_TOKEN", "change-me-before-deploying")
store = Store(os.getenv("DATABASE_PATH", "invoice_agent.sqlite3"))
app = FastAPI(title=APP_NAME, version=APP_VERSION)

def err(code, status, message="Request could not be processed"):
    return JSONResponse(status_code=status, content={"error":{"code":code,"message":message}})

def principal_for(auth):
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    token = auth[7:]
    if not token or token != TOKEN:
        raise HTTPException(403, "Forbidden")
    return hashlib.sha256(token.encode()).hexdigest()

def check_post(request):
    if request.headers.get("A2A-Version") != "1.0":
        raise HTTPException(400, "Unsupported A2A version")
    if request.headers.get("content-type","").split(";")[0].strip().lower() != "application/a2a+json":
        raise HTTPException(415, "Unsupported media type")

def part(media, data): return {"mediaType":media,"data":data}
def task_obj(tid,cid,state,history,artifacts,metadata=None):
    x={"id":tid,"contextId":cid,"status":{"state":state},"history":history,"artifacts":artifacts}
    if metadata: x["metadata"]=metadata
    return x

@app.get("/.well-known/agent-card.json")
def agent_card():
    return {
      "name":APP_NAME,"description":"Evidence-based invoice action proposals with approval-gated receipts.",
      "version":APP_VERSION,"capabilities":{"streaming":False,"pushNotifications":False},
      "supportedInterfaces":[{"url":BASE_URL,"protocolBinding":"HTTP+JSON","protocolVersion":"1.0"}],
      "defaultInputModes":[BATCH_MEDIA,RESULTS_MEDIA],"defaultOutputModes":[PROPOSAL_MEDIA,RECEIPT_MEDIA],
      "skills":[{"id":"invoice_action_agent","name":"Invoice Action Agent",
        "description":"Analyze invoice packages and propose safe actions with evidence.",
        "tags":["invoice","accounts-payable","evidence","approval"]}]
    }

@app.post("/a2a/message:send")
async def send_message(request: Request, authorization: str | None = Header(default=None)):
    principal=principal_for(authorization); check_post(request)
    try: body=await request.json()
    except Exception: raise HTTPException(400,"Invalid JSON")
    msg=body.get("message")
    if not isinstance(msg,dict): raise HTTPException(400,"Missing message")
    mid=msg.get("messageId")
    if not isinstance(mid,str) or not mid: raise HTTPException(400,"Missing messageId")
    digest=hashlib.sha256(canonical_json(msg).encode()).hexdigest()
    old=store.get_idempotency(principal,mid)
    if old:
        if old["message_hash"] != digest: return err("IDEMPOTENCY_CONFLICT",409)
        return JSONResponse(content={"task":json.loads(old["response_json"])})
    parts=msg.get("parts") or []
    if len(parts)!=1 or not isinstance(parts[0],dict): raise HTTPException(400,"Expected one message part")
    if msg.get("role")!="ROLE_USER": raise HTTPException(400,"Expected ROLE_USER")
    media,data=parts[0].get("mediaType"),parts[0].get("data")
    if media==BATCH_MEDIA:
        try: batch=validate_batch(data)
        except ValueError as e: raise HTTPException(400,str(e))
        cached={}; missing=[]
        for p in batch["packages"]:
            key=hashlib.sha256(canonical_json(p).encode()).hexdigest()
            d=store.get_decision(key)
            if d: cached[p["packageId"]]=d
            else: missing.append(p)
        if missing:
            decided=await decide_batch(batch,missing)
            byid={p["packageId"]:p for p in decided}
            for p in missing:
                if p["packageId"] not in byid: raise HTTPException(502,"AI omitted a package")
                d=byid[p["packageId"]]
                try: validate_proposals({"packages":[p]},[d])
                except ValueError as e: raise HTTPException(502,f"Invalid AI proposal: {e}")
                store.put_decision(hashlib.sha256(canonical_json(p).encode()).hexdigest(),d)
                cached[p["packageId"]]=d
        try: proposals=validate_proposals(batch,[cached[p["packageId"]] for p in batch["packages"]])
        except ValueError as e: raise HTTPException(502,f"Invalid proposals: {e}")
        tid,cid=str(uuid.uuid4()),msg.get("contextId") or str(uuid.uuid4())
        task=task_obj(tid,cid,"TASK_STATE_INPUT_REQUIRED",[msg],[{"name":"invoice-action-proposals","parts":[part(PROPOSAL_MEDIA,{"batchId":batch["batchId"],"proposals":proposals})]}],{"principal":principal,"batchId":batch["batchId"]})
        store.create_task(tid,cid,principal,batch["batchId"],"TASK_STATE_INPUT_REQUIRED",task)
        store.put_idempotency(principal,mid,digest,tid,task)
        return JSONResponse(content={"task":task})
    if media==RESULTS_MEDIA:
        tid,cid=msg.get("taskId"),msg.get("contextId")
        if not tid or not cid: raise HTTPException(400,"Missing taskId or contextId")
        row=store.get_task(tid,principal)
        if not row: return err("TASK_NOT_FOUND",404)
        task=json.loads(row["task_json"])
        if task.get("contextId")!=cid: return err("TASK_NOT_FOUND",404)
        if row["state"] in ("COMPLETED","CANCELED"): return err("TASK_TERMINAL",409)
        try: batch_id,results=validate_results(data)
        except ValueError as e: raise HTTPException(400,str(e))
        if batch_id!=row["batch_id"]: return err("RESULT_BINDING_MISMATCH",409)
        proposal_data=task["artifacts"][0]["parts"][0]["data"]
        proposals={p["packageId"]:p for p in proposal_data["proposals"]}
        executions=[]
        for r in results:
            p=proposals.get(r["packageId"])
            if not p or p["actionId"]!=r["actionId"] or p["action"]!=r["action"]:
                return err("RESULT_BINDING_MISMATCH",409)
            if r["outcome"]=="ACCEPTED":
                executions.append({"packageId":p["packageId"],"actionId":p["actionId"],"action":p["action"],
                  "receiptNonce":r["receiptNonce"],"facts":p["facts"],"evidenceRefs":p["evidenceRefs"]})
        completed=task_obj(tid,cid,"COMPLETED",task["history"]+[msg],task["artifacts"]+[{"name":"invoice-action-receipts","parts":[part(RECEIPT_MEDIA,{"batchId":batch_id,"executions":executions})]}],task.get("metadata"))
        if not store.finish_task(tid,principal,"COMPLETED",completed): return err("TASK_TERMINAL",409)
        return JSONResponse(content={"task":completed})
    raise HTTPException(415,"Unsupported message part media type")

@app.get("/a2a/tasks/{task_id}")
def get_task(task_id:str, authorization:str|None=Header(default=None), a2a_version:str|None=Header(default=None,alias="A2A-Version")):
    principal=principal_for(authorization)
    if a2a_version!="1.0": raise HTTPException(400,"Unsupported A2A version")
    row=store.get_task(task_id,principal)
    if not row: return err("TASK_NOT_FOUND",404)
    return JSONResponse(content={"task":json.loads(row["task_json"])})

@app.get("/a2a/tasks")
def list_tasks(authorization:str|None=Header(default=None), a2a_version:str|None=Header(default=None,alias="A2A-Version")):
    principal=principal_for(authorization)
    if a2a_version!="1.0": raise HTTPException(400,"Unsupported A2A version")
    return {"tasks":[json.loads(r["task_json"]) for r in store.list_tasks(principal)]}

@app.post("/a2a/tasks/{task_id}:cancel")
async def cancel_task(task_id:str, request:Request, authorization:str|None=Header(default=None)):
    principal=principal_for(authorization); check_post(request)
    row=store.get_task(task_id,principal)
    if not row: return err("TASK_NOT_FOUND",404)
    task=json.loads(row["task_json"])
    canceled=task_obj(task_id,task["contextId"],"CANCELED",task["history"],task["artifacts"],task.get("metadata"))
    if not store.finish_task(task_id,principal,"CANCELED",canceled): return err("TASK_TERMINAL",409)
    return JSONResponse(content=canceled)
