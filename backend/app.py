import hashlib, secrets, sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 撤映口径（五处同态）：本文件只实现甲路径。
# 甲：撤映同一事务内迁入冲突并删除正式；迁入或删除任一失败则两者一起回滚。
# 乙（正式保留打 orphan 标记）与本路径互斥，不得混用。
REVOKE_PATH = "甲"

STATE_REGISTERED = "在册"
STATE_REVOKED = "已撤映"
FORMAL_IN = "在正式"
FORMAL_OUT = "不在正式"
CONFLICT_IN = "在冲突"
CONFLICT_OUT = "不在冲突"
VERDICT_OK = "一致"
VERDICT_BAD = "不一致"


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def _active_conflict(conn, old_key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM conflicts WHERE old_key=? AND reason='撤映' AND status='待处置' LIMIT 1",
        (old_key,),
    ).fetchone() is not None


def _refresh_card(conn, version: str, old_key: str, entity_key: str, at: str):
    """按当前正式区/冲突区/映射册的实际状态刷新互证卡。"""
    formal = conn.execute(
        "SELECT 1 FROM formal_entities WHERE mapped_from=? AND entity_key=? LIMIT 1",
        (old_key, entity_key),
    ).fetchone() is not None
    conflict = _active_conflict(conn, old_key)
    row = conn.execute(
        "SELECT state FROM mapping_register WHERE version=? AND old_key=?",
        (version, old_key),
    ).fetchone()
    reg_state = row["state"] if row else STATE_REGISTERED
    if formal and not conflict and reg_state == STATE_REGISTERED:
        verdict = VERDICT_OK
    elif (not formal) and conflict and reg_state == STATE_REVOKED:
        verdict = VERDICT_OK
    else:
        verdict = VERDICT_BAD
    conn.execute(
        """INSERT INTO reconcile_cards(
               old_key, entity_key, version, formal_state, conflict_state,
               register_state, verdict, checked_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(version, old_key) DO UPDATE SET
               entity_key=excluded.entity_key,
               formal_state=excluded.formal_state,
               conflict_state=excluded.conflict_state,
               register_state=excluded.register_state,
               verdict=excluded.verdict,
               checked_at=excluded.checked_at""",
        (old_key, entity_key, version,
         FORMAL_IN if formal else FORMAL_OUT,
         CONFLICT_IN if conflict else CONFLICT_OUT,
         reg_state, verdict, at),
    )
    return verdict


def _audit(conn, action: str, old_key: str, entity_key: str | None,
           outcome: str, detail: str, at: str):
    conn.execute(
        "INSERT INTO disposition_audit(action,old_key,entity_key,outcome,detail,at) VALUES (?,?,?,?,?,?)",
        (action, old_key, entity_key, outcome, detail, at),
    )


def _audit_commit(action: str, old_key: str, entity_key: str | None,
                  outcome: str, detail: str, at: str):
    """独立事务落审计：主事务回滚时审计仍可追溯。"""
    c = connect()
    try:
        c.execute("PRAGMA busy_timeout = 3000")
        _audit(c, action, old_key, entity_key, outcome, detail, at)
        c.commit()
    finally:
        c.close()


LOCK_TTL_SECONDS = 120


def _acquire_lock(entity_key: str, owner: str, now: str) -> bool:
    """独立连接立即提交占锁，保证并发后到者可见；过期锁可被接管。"""
    c = connect()
    try:
        c.execute("PRAGMA busy_timeout = 5000")
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        row = c.execute("SELECT owner, acquired_at FROM revoke_locks WHERE entity_key=?",
                        (entity_key,)).fetchone()
        if row is not None:
            try:
                aged = datetime.fromisoformat(row["acquired_at"]) + timedelta(seconds=LOCK_TTL_SECONDS)
            except ValueError:
                aged = datetime.min
            if aged > datetime.now():
                c.execute("ROLLBACK")
                return False
            c.execute("DELETE FROM revoke_locks WHERE entity_key=?", (entity_key,))
        c.execute("INSERT INTO revoke_locks(entity_key,owner,acquired_at) VALUES (?,?,?)",
                  (entity_key, owner, now))
        c.execute("COMMIT")
        return True
    finally:
        c.close()


def _release_lock(entity_key: str, owner: str):
    """只释放自己持有的锁，不误删他路锁。"""
    c = connect()
    try:
        c.execute("PRAGMA busy_timeout = 3000")
        c.execute("DELETE FROM revoke_locks WHERE entity_key=? AND owner=?", (entity_key, owner))
        c.commit()
    finally:
        c.close()


def _backfill_five(conn):
    """老库补映射册/互证卡，不覆盖已有状态。"""
    rows = conn.execute("SELECT * FROM formal_entities ORDER BY id").fetchall()
    for f in rows:
        conn.execute(
            "INSERT OR IGNORE INTO mapping_register(version,old_key,new_key,state,updated_at) VALUES (?,?,?,?,?)",
            (f["version"], f["mapped_from"], f["entity_key"], STATE_REGISTERED, f["created_at"]),
        )
        exists = conn.execute(
            "SELECT 1 FROM reconcile_cards WHERE version=? AND old_key=?",
            (f["version"], f["mapped_from"]),
        ).fetchone()
        if not exists:
            _refresh_card(conn, f["version"], f["mapped_from"], f["entity_key"], f["created_at"])


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mapping_register(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          state TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(version, old_key)
        );
        CREATE TABLE IF NOT EXISTS reconcile_cards(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          entity_key TEXT NOT NULL,
          version TEXT NOT NULL,
          formal_state TEXT NOT NULL,
          conflict_state TEXT NOT NULL,
          register_state TEXT NOT NULL,
          verdict TEXT NOT NULL,
          checked_at TEXT NOT NULL,
          UNIQUE(version, old_key)
        );
        CREATE TABLE IF NOT EXISTS disposition_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          action TEXT NOT NULL,
          old_key TEXT NOT NULL,
          entity_key TEXT,
          outcome TEXT NOT NULL,
          detail TEXT NOT NULL,
          at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revoke_locks(
          entity_key TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_formal_origin ON formal_entities(mapped_from, entity_key);
        CREATE INDEX IF NOT EXISTS idx_conflicts_origin ON conflicts(old_key, reason, status);
        CREATE INDEX IF NOT EXISTS idx_audit_at ON disposition_audit(at);
        """)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11", "生效", "2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11", "OLD-A1", "NEW-100", "一对一"),
                ("m-2026.11", "OLD-B1", "NEW-200", "多对一"),
                ("m-2026.11", "OLD-B2", "NEW-200", "多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100", "OLD-A1", "m-2026.11", "2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9", "无映射", "挂起"))
        _backfill_five(conn)


@app.on_event("startup")
def _s():
    init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n,
            "revoke_path": REVOKE_PATH, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?",
                        (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}


@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}


class RemapIn(BaseModel):
    old_key: str


@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = now_iso()
    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver:
            raise HTTPException(404, "无生效映射版本")
        m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
        if not m:
            c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
            c.commit()
            raise HTTPException(409, "无映射，已进冲突队列")
        c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                  (m["new_key"], body.old_key, ver["version"], now))
        c.execute("UPDATE conflicts SET status='已重映' WHERE old_key=? AND reason='撤映' AND status='待处置'",
                  (body.old_key,))
        # 重新入正式区即解除该实体的撤映锁，使后续再撤可重新占锁，不误记争用。
        c.execute("DELETE FROM revoke_locks WHERE entity_key=?", (m["new_key"],))
        c.execute(
            """INSERT INTO mapping_register(version,old_key,new_key,state,updated_at) VALUES (?,?,?,?,?)
               ON CONFLICT(version, old_key) DO UPDATE SET
                   new_key=excluded.new_key, state=excluded.state, updated_at=excluded.updated_at""",
            (ver["version"], body.old_key, m["new_key"], STATE_REGISTERED, now),
        )
        _refresh_card(c, ver["version"], body.old_key, m["new_key"], now)
        _audit(c, "重映", body.old_key, m["new_key"], "成功", "重映进入正式区并同步映射册/互证卡", now)
        return {"entity_key": m["new_key"], "version": ver["version"]}


@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}


@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}


@app.get("/api/register")
def register(authorization: str | None = Header(default=None)):
    """映射册（按实体登记，撤映后仍留痕）。"""
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM mapping_register ORDER BY id DESC").fetchall()]}


@app.get("/api/reconcile-cards")
def reconcile_cards(authorization: str | None = Header(default=None)):
    """互证卡：正式区、冲突区、映射册三源同态核对。"""
    require_user(authorization)
    with connect() as c:
        items = [dict(r) for r in c.execute("SELECT * FROM reconcile_cards ORDER BY id DESC").fetchall()]
    bad = sum(1 for x in items if x["verdict"] != VERDICT_OK)
    return {"items": items, "inconsistent": bad}


@app.get("/api/audit")
def audit(authorization: str | None = Header(default=None)):
    """处置审计。"""
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM disposition_audit ORDER BY id DESC").fetchall()]}


@app.get("/api/orphans")
def orphans(authorization: str | None = Header(default=None)):
    """孤儿只读查询：映射册仍在册，但正式区与冲突区都找不到。"""
    require_user(authorization)
    with connect() as c:
        items = [dict(r) for r in c.execute(
            """SELECT r.* FROM mapping_register r
               WHERE r.state=?
                 AND NOT EXISTS (SELECT 1 FROM formal_entities f
                                 WHERE f.mapped_from=r.old_key AND f.entity_key=r.new_key)
                 AND NOT EXISTS (SELECT 1 FROM conflicts c
                                 WHERE c.old_key=r.old_key AND c.reason='撤映' AND c.status='待处置')
               ORDER BY r.id DESC""",
            (STATE_REGISTERED,),
        ).fetchall()]
    return {"items": items, "count": len(items)}


class RevokeIn(BaseModel):
    old_key: str | None = None
    entity_key: str | None = None


@app.post("/api/revoke")
def revoke(body: RevokeIn, authorization: str | None = Header(default=None)):
    """撤映，甲路径：同一事务迁入冲突区并删除正式区，失败一并回滚；两路抢撤占锁，后到记争用。"""
    require_user(authorization)
    now = now_iso()
    owner = authorization.removeprefix("Bearer ").strip()[:16]

    # 先做只读解析，确定 old_key/entity_key/版本，再进入写事务。
    rconn = connect()
    try:
        ver = rconn.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver:
            raise HTTPException(404, "无生效映射版本")
        old_key = body.old_key
        if not old_key:
            if not body.entity_key:
                raise HTTPException(422, "须提供 old_key 或 entity_key")
            origins = {r["mapped_from"] for r in rconn.execute(
                "SELECT DISTINCT mapped_from FROM formal_entities WHERE entity_key=?", (body.entity_key,)).fetchall()}
            if len(origins) != 1:
                raise HTTPException(400, "实体对应多个旧键，须指定 old_key")
            old_key = origins.pop()
        m = rconn.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                          (ver["version"], old_key)).fetchone()
        if not m:
            raise HTTPException(404, "无映射")
        entity_key = m["new_key"]
    finally:
        rconn.close()

    def _contended():
        _audit_commit("撤映", old_key, entity_key, "争用",
                      "争用：该实体已有待处置撤映记录或另一路正在撤映，后到不重复执行", now)
        return JSONResponse(status_code=409, content={
            "detail": "撤映争用：同一实体已撤映或另一路正在撤映",
            "contended": True, "old_key": old_key, "entity_key": entity_key, "path": REVOKE_PATH,
        })

    # 占锁：独立连接立即提交，后到者可见；锁行唯一，故并发后到必判争用。
    if not _acquire_lock(entity_key, owner, now):
        return _contended()

    try:
        conn = connect()
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")

            # 已有待处置的撤映记录 → 该实体已被撤映，后到记争用，不重复迁入。
            if conn.execute("SELECT 1 FROM conflicts WHERE old_key=? AND reason='撤映' AND status='待处置'",
                            (old_key,)).fetchone():
                conn.execute("ROLLBACK")
                conn.close()
                _release_lock(entity_key, owner)
                return _contended()

            # 甲：先迁入冲突区，再删除正式区，同事务。
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (old_key, "撤映", "待处置"))
            conn.execute(
                """INSERT INTO mapping_register(version,old_key,new_key,state,updated_at) VALUES (?,?,?,?,?)
                   ON CONFLICT(version, old_key) DO UPDATE SET
                       new_key=excluded.new_key, state=excluded.state, updated_at=excluded.updated_at""",
                (ver["version"], old_key, entity_key, STATE_REVOKED, now),
            )
            cur = conn.execute("DELETE FROM formal_entities WHERE mapped_from=? AND entity_key=?",
                               (old_key, entity_key))
            if cur.rowcount == 0:
                raise HTTPException(404, "正式区无该实体")
            _refresh_card(conn, ver["version"], old_key, entity_key, now)
            _audit(conn, "撤映", old_key, entity_key, "成功", "甲路径：同事务迁冲突并删正式，五处同态", now)
            conn.execute("COMMIT")
            return {"old_key": old_key, "entity_key": entity_key, "path": REVOKE_PATH, "orphaned": False}
        except HTTPException:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            finally:
                conn.close()
            raise
        except Exception as exc:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            finally:
                conn.close()
            # 主事务回滚后独立落失败审计，保证账可追溯。
            _audit_commit("撤映", old_key, entity_key, "失败",
                          f"甲路径事务失败，迁入与删除一并回滚：{type(exc).__name__}", now)
            raise HTTPException(500, "撤映失败，事务已回滚")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    finally:
        # 无论成败都释放本次占锁，避免锁行残留把后续重映后的再撤误判为争用。
        _release_lock(entity_key, owner)


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
