import sqlite3, json
from schemas import canonical_json

class Store:
    def __init__(self,path):
        self.path=path
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,context_id TEXT NOT NULL,principal TEXT NOT NULL,batch_id TEXT NOT NULL,state TEXT NOT NULL,task_json TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS tasks_owner_idx ON tasks(principal,created_at);
            CREATE TABLE IF NOT EXISTS idempotency(principal TEXT NOT NULL,message_id TEXT NOT NULL,message_hash TEXT NOT NULL,task_id TEXT NOT NULL,response_json TEXT NOT NULL,PRIMARY KEY(principal,message_id));
            CREATE TABLE IF NOT EXISTS decisions(package_hash TEXT PRIMARY KEY,decision_json TEXT NOT NULL);
            """)
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20,isolation_level=None); db.row_factory=sqlite3.Row
        return db
    def get_idempotency(self,p,m):
        with self.connect() as db: return db.execute("SELECT * FROM idempotency WHERE principal=? AND message_id=?",(p,m)).fetchone()
    def put_idempotency(self,p,m,h,t,task):
        with self.connect() as db: db.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)",(p,m,h,t,canonical_json(task)))
    def create_task(self,tid,cid,p,b,s,task):
        with self.connect() as db: db.execute("INSERT INTO tasks(task_id,context_id,principal,batch_id,state,task_json) VALUES(?,?,?,?,?,?)",(tid,cid,p,b,s,canonical_json(task)))
    def get_task(self,tid,p):
        with self.connect() as db: return db.execute("SELECT * FROM tasks WHERE task_id=? AND principal=?",(tid,p)).fetchone()
    def list_tasks(self,p):
        with self.connect() as db: return db.execute("SELECT * FROM tasks WHERE principal=? ORDER BY created_at DESC",(p,)).fetchall()
    def finish_task(self,tid,p,state,task):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT state FROM tasks WHERE task_id=? AND principal=?",(tid,p)).fetchone()
            if not row or row["state"] in ("COMPLETED","CANCELED"):
                db.execute("ROLLBACK"); return False
            db.execute("UPDATE tasks SET state=?,task_json=? WHERE task_id=? AND principal=?",(state,canonical_json(task),tid,p))
            db.execute("COMMIT"); return True
    def get_decision(self,h):
        with self.connect() as db:
            r=db.execute("SELECT decision_json FROM decisions WHERE package_hash=?",(h,)).fetchone()
            return json.loads(r["decision_json"]) if r else None
    def put_decision(self,h,d):
        with self.connect() as db: db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?)",(h,canonical_json(d)))
