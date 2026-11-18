"""
NEXUS AI · mock_servicenow/app.py
A local stand-in for ServiceNow, for development and demos. It copies the parts
of ServiceNow's Table API that NEXUS uses, with the same URLs, field names,
query syntax and response shapes, so the NEXUS ServiceNow connector works
against it unchanged.

    GET    /api/now/table/{table}            sysparm_query / _fields / _limit / _offset / _display_value
    GET    /api/now/table/{table}/{sys_id}
    POST   /api/now/table/{table}
    PATCH  /api/now/table/{table}/{sys_id}   work_notes / comments go to sys_journal_field
    PUT    /api/now/table/{table}/{sys_id}
    DELETE /api/now/table/{table}/{sys_id}
    POST   /oauth_token.do                   OAuth tokens: client_credentials and password grants

MOCK_SN_BASIC_AUTH=false makes the API refuse password logins, like instances
that only allow OAuth.

Plus a small web page at / to create and update incidents by hand, and an
optional webhook that pushes changed incidents to NEXUS (like a Business Rule).

This is not ServiceNow and not affiliated with it; it only mimics the API shape.
"""

import base64
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

DB_PATH = os.environ.get("MOCK_SN_DB", "/data/servicenow.db")
API_USER = os.environ.get("MOCK_SN_USER", "nexus.integration")
API_PASSWORD = os.environ.get("MOCK_SN_PASSWORD", "")
API_TOKEN = os.environ.get("MOCK_SN_TOKEN", "")
CLIENT_ID = os.environ.get("MOCK_SN_CLIENT_ID", "nexus-mock-client")
CLIENT_SECRET = os.environ.get("MOCK_SN_CLIENT_SECRET", "")
TOKEN_TTL = int(os.environ.get("MOCK_SN_TOKEN_TTL", "1800"))
BASIC_AUTH = os.environ.get("MOCK_SN_BASIC_AUTH", "true").lower() != "false"
WEBHOOK_URL = os.environ.get("MOCK_SN_WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("MOCK_SN_WEBHOOK_TOKEN", "")
SEED = os.environ.get("MOCK_SN_SEED", "demo")

SN_DT = "%Y-%m-%d %H:%M:%S"

# Reference fields → (referenced table, field shown as the display value)
REFERENCES = {
    "caller_id": ("sys_user", "name"),
    "opened_by": ("sys_user", "name"),
    "assigned_to": ("sys_user", "name"),
    "assignment_group": ("sys_user_group", "name"),
}

CHOICES = {
    "state": {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"},
    "priority": {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"},
    "impact": {"1": "1 - High", "2": "2 - Medium", "3": "3 - Low"},
    "urgency": {"1": "1 - High", "2": "2 - Medium", "3": "3 - Low"},
    "category": {"inquiry": "Inquiry / Help", "software": "Software", "hardware": "Hardware",
                 "network": "Network", "database": "Database", "security": "Security",
                 "access": "Access", "performance": "Performance"},
}

JOURNAL_FIELDS = {"work_notes", "comments"}
SYSTEM_FIELDS = {"sys_id", "sys_created_on", "sys_updated_on", "sys_created_by",
                 "sys_updated_by", "sys_mod_count", "number"}
PREFIX = {"incident": "INC", "problem": "PRB", "sc_request": "REQ", "change_request": "CHG"}

app = FastAPI(title="Mock ServiceNow (NEXUS AI local testing)", docs_url="/mock/docs")
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────
#  Storage (SQLite, one JSON document per record)
# ─────────────────────────────────────────────────────────────
@contextmanager
def db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS records (tbl TEXT, sys_id TEXT, data TEXT, PRIMARY KEY (tbl, sys_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS counters (tbl TEXT PRIMARY KEY, value INTEGER)")


def now_str() -> str:
    return datetime.now(timezone.utc).strftime(SN_DT)


def all_records(tbl: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT data FROM records WHERE tbl = ?", (tbl,)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def get_record(tbl: str, sys_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT data FROM records WHERE tbl = ? AND sys_id = ?", (tbl, sys_id)).fetchone()
    return json.loads(row["data"]) if row else None


def save_record(tbl: str, record: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO records (tbl, sys_id, data) VALUES (?, ?, ?)",
                     (tbl, record["sys_id"], json.dumps(record)))


def delete_record(tbl: str, sys_id: str) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM records WHERE tbl = ? AND sys_id = ?", (tbl, sys_id))
    return cur.rowcount > 0


def next_number(tbl: str) -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM counters WHERE tbl = ?", (tbl,)).fetchone()
        value = (row["value"] if row else 10000) + 1
        conn.execute("INSERT OR REPLACE INTO counters (tbl, value) VALUES (?, ?)", (tbl, value))
    return f"{PREFIX.get(tbl, tbl[:3].upper())}{value:07d}"


def bump_counter_past(tbl: str, number: str) -> None:
    """Keep generated numbers above any number that was imported."""
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return
    with db() as conn:
        row = conn.execute("SELECT value FROM counters WHERE tbl = ?", (tbl,)).fetchone()
        if not row or row["value"] < int(digits):
            conn.execute("INSERT OR REPLACE INTO counters (tbl, value) VALUES (?, ?)", (tbl, int(digits)))


# ─────────────────────────────────────────────────────────────
#  Record helpers
# ─────────────────────────────────────────────────────────────
def compute_priority(record: dict[str, Any]) -> None:
    """ServiceNow derives priority from impact × urgency (1 = highest)."""
    try:
        impact, urgency = int(record.get("impact") or 3), int(record.get("urgency") or 3)
        record["priority"] = str(min(5, impact + urgency - 1))
    except ValueError:
        pass


def add_journal(element_id: str, tbl: str, element: str, value: str, user: str) -> None:
    if not value:
        return
    ts = now_str()
    save_record("sys_journal_field", {
        "sys_id": uuid.uuid4().hex, "element_id": element_id, "name": tbl, "element": element,
        "value": value, "sys_created_on": ts, "sys_created_by": user,
        "sys_updated_on": ts, "sys_updated_by": user, "sys_mod_count": "0",
    })


def journal_for(sys_id: str) -> list[dict[str, Any]]:
    entries = [j for j in all_records("sys_journal_field") if j.get("element_id") == sys_id]
    return sorted(entries, key=lambda j: j["sys_created_on"], reverse=True)


def display_of(field: str, value: str) -> str:
    if field in REFERENCES and value:
        ref_tbl, ref_field = REFERENCES[field]
        ref = get_record(ref_tbl, value)
        return str(ref.get(ref_field, "")) if ref else ""
    if field in CHOICES:
        return CHOICES[field].get(str(value), str(value))
    return value


def render(record: dict[str, Any], tbl: str, fields: list[str] | None, mode: str,
           exclude_ref_link: bool, base_url: str) -> dict[str, Any]:
    keys = fields or list(record.keys())
    out: dict[str, Any] = {}
    for f in keys:
        if f not in record and f not in REFERENCES:
            if fields:          # asked for an unknown field: ServiceNow returns ""
                out[f] = "" if mode != "all" else {"display_value": "", "value": ""}
            continue
        value = "" if record.get(f) is None else str(record.get(f))
        disp = display_of(f, value)
        link = None
        if f in REFERENCES and value and not exclude_ref_link:
            link = f"{base_url}/api/now/table/{REFERENCES[f][0]}/{value}"
        if mode == "all":
            item: dict[str, Any] = {"display_value": disp, "value": value}
            if link:
                item["link"] = link
            out[f] = item
        elif mode == "true":
            out[f] = disp
        elif link:
            out[f] = {"link": link, "value": value}
        else:
            out[f] = value
    return out


# ─────────────────────────────────────────────────────────────
#  Encoded query (the subset of sysparm_query NEXUS and people commonly use)
# ─────────────────────────────────────────────────────────────
OPERATORS = ["ISNOTEMPTY", "ISEMPTY", "STARTSWITH", "ENDSWITH", "NOT IN", "LIKE", "IN",
             ">=", "<=", "!=", ">", "<", "="]


def field_value(record: dict[str, Any], field: str) -> str:
    if "." in field:                            # e.g. assignment_group.name
        ref_field, sub = field.split(".", 1)
        if ref_field in REFERENCES:
            ref = get_record(REFERENCES[ref_field][0], str(record.get(ref_field) or ""))
            return str(ref.get(sub, "")) if ref else ""
    return "" if record.get(field) is None else str(record.get(field))


def compare(a: str, b: str) -> int:
    try:
        x, y = float(a), float(b)
    except ValueError:
        x, y = a, b
    return (x > y) - (x < y)


def matches(record: dict[str, Any], condition: str) -> bool:
    for op in OPERATORS:
        idx = condition.find(op)
        if idx > 0:
            field, value = condition[:idx], condition[idx + len(op):]
            actual = field_value(record, field)
            if op == "ISEMPTY":
                return actual == ""
            if op == "ISNOTEMPTY":
                return actual != ""
            if op == "STARTSWITH":
                return actual.lower().startswith(value.lower())
            if op == "ENDSWITH":
                return actual.lower().endswith(value.lower())
            if op == "LIKE":
                return value.lower() in actual.lower()
            if op == "IN":
                return actual in value.split(",")
            if op == "NOT IN":
                return actual not in value.split(",")
            c = compare(actual, value)
            return {"=": c == 0, "!=": c != 0, ">": c > 0, "<": c < 0, ">=": c >= 0, "<=": c <= 0}[op]
    return True


def apply_query(records: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    order: list[tuple[str, bool]] = []
    groups: list[list[str]] = []           # OR-groups of AND-conditions: a^b^ORc → [[a], [b, c]]
    for part in [p for p in (query or "").split("^") if p]:
        if part.startswith("ORDERBYDESC"):
            order.append((part[len("ORDERBYDESC"):], True))
        elif part.startswith("ORDERBY"):
            order.append((part[len("ORDERBY"):], False))
        elif part.startswith("OR") and groups:
            groups[-1].append(part[2:])
        else:
            groups.append([part])
    result = [r for r in records if all(any(matches(r, c) for c in g) for g in groups)]
    for field, desc in reversed(order):
        result.sort(key=lambda r: field_value(r, field), reverse=desc)
    return result


# ─────────────────────────────────────────────────────────────
#  Auth (Basic or Bearer, like ServiceNow)
# ─────────────────────────────────────────────────────────────
_tokens: dict[str, tuple[str, float]] = {}      # access token → (user, expires at)
TOKEN_REQUESTS = {"count": 0}                   # tests check that tokens are reused


def auth_user(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        issued = _tokens.get(header[7:])
        if issued and issued[1] > time.time():
            return issued[0]
    if header.lower().startswith("basic ") and API_PASSWORD and BASIC_AUTH:
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            user, pw = "", ""
        if secrets.compare_digest(user, API_USER) and secrets.compare_digest(pw, API_PASSWORD):
            return user
    if header.lower().startswith("bearer ") and API_TOKEN and secrets.compare_digest(header[7:], API_TOKEN):
        return API_USER
    raise HTTPException(status_code=401, detail="auth")


@app.exception_handler(HTTPException)
async def sn_error(request: Request, exc: HTTPException):
    messages = {401: ("User Not Authenticated", "Required to provide Auth information"),
                404: ("No Record found", "Record doesn't exist or ACL restricts the record retrieval"),
                400: ("Invalid request", str(exc.detail))}
    message, detail = messages.get(exc.status_code, (str(exc.detail), ""))
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"message": message, "detail": detail}, "status": "failure"})


# ─────────────────────────────────────────────────────────────
#  Webhook push (like a Business Rule calling NEXUS)
# ─────────────────────────────────────────────────────────────
def push_webhook(tbl: str, record: dict[str, Any]) -> None:
    if not WEBHOOK_URL or tbl != "incident":
        return
    payload = {"result": render(record, tbl, None, "all", True, "")}

    def send():
        try:
            httpx.post(WEBHOOK_URL, json=payload, timeout=5,
                       headers={"X-Nexus-Webhook-Token": WEBHOOK_TOKEN})
        except httpx.HTTPError as e:
            print(f"[mock-servicenow] webhook to {WEBHOOK_URL} failed: {e}", flush=True)

    threading.Thread(target=send, daemon=True).start()


# ─────────────────────────────────────────────────────────────
#  Core write logic (used by the API and the web page)
# ─────────────────────────────────────────────────────────────
def create(tbl: str, body: dict[str, Any], user: str, allow_system_fields: bool = False) -> dict[str, Any]:
    ts = now_str()
    journal = {k: body.pop(k) for k in list(body) if k in JOURNAL_FIELDS}
    sys_id = str(body.pop("sys_id", "") or uuid.uuid4().hex)
    if not re.fullmatch(r"[0-9a-f]{32}", sys_id):
        raise HTTPException(status_code=400, detail="sys_id must be 32 lowercase hex characters")
    if get_record(tbl, sys_id):
        raise HTTPException(status_code=400, detail=f"Record {sys_id} already exists")
    system = {k: body.pop(k) for k in list(body) if k in SYSTEM_FIELDS}
    record = {k: "" if v is None else str(v) for k, v in body.items()}
    if tbl == "incident":
        record.setdefault("state", "1")
        record.setdefault("impact", "3")
        record.setdefault("urgency", "3")
        if "priority" not in body:
            compute_priority(record)
    number = system.get("number") if allow_system_fields and system.get("number") else None
    if tbl in PREFIX:
        record["number"] = number or next_number(tbl)
        if number:
            bump_counter_past(tbl, number)
    record.update({
        "sys_id": sys_id,
        "sys_created_on": system.get("sys_created_on") if allow_system_fields and system.get("sys_created_on") else ts,
        "sys_updated_on": system.get("sys_updated_on") if allow_system_fields and system.get("sys_updated_on") else ts,
        "sys_created_by": user, "sys_updated_by": user, "sys_mod_count": "0",
    })
    save_record(tbl, record)
    for element, value in journal.items():
        add_journal(sys_id, tbl, element, str(value), user)
    push_webhook(tbl, record)
    return record


def update(tbl: str, sys_id: str, body: dict[str, Any], user: str) -> dict[str, Any]:
    record = get_record(tbl, sys_id)
    if not record:
        raise HTTPException(status_code=404, detail="not found")
    journal = {k: body.pop(k) for k in list(body) if k in JOURNAL_FIELDS}
    changed = False
    for key, value in body.items():
        if key in SYSTEM_FIELDS:
            continue
        value = "" if value is None else str(value)
        if record.get(key) != value:
            record[key] = value
            changed = True
    if tbl == "incident" and ("impact" in body or "urgency" in body) and "priority" not in body:
        compute_priority(record)
    record["sys_updated_on"] = now_str()
    record["sys_updated_by"] = user
    record["sys_mod_count"] = str(int(record.get("sys_mod_count") or 0) + 1)
    save_record(tbl, record)
    for element, value in journal.items():
        add_journal(sys_id, tbl, element, str(value), user)
    if changed:          # a note alone doesn't trigger the "record changed" webhook
        push_webhook(tbl, record)
    return record


# ─────────────────────────────────────────────────────────────
#  Table API
# ─────────────────────────────────────────────────────────────
def _fields(param: str | None) -> list[str] | None:
    return [f.strip() for f in param.split(",") if f.strip()] if param else None


def _flags(request: Request) -> tuple[str, bool]:
    mode = (request.query_params.get("sysparm_display_value") or "false").lower()
    exclude = (request.query_params.get("sysparm_exclude_reference_link") or "false").lower() == "true"
    return (mode if mode in ("true", "false", "all") else "false"), exclude


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/api/now/table/{tbl}")
def list_records(tbl: str, request: Request):
    auth_user(request)
    q = request.query_params
    records = apply_query(all_records(tbl), q.get("sysparm_query", ""))
    total = len(records)
    try:
        offset = max(0, int(q.get("sysparm_offset") or 0))
        limit = max(1, min(int(q.get("sysparm_limit") or 10000), 10000))
    except ValueError:
        raise HTTPException(status_code=400, detail="sysparm_limit/offset must be numbers")
    page = records[offset:offset + limit]
    mode, exclude = _flags(request)
    fields = _fields(q.get("sysparm_fields"))
    result = [render(r, tbl, fields, mode, exclude, _base(request)) for r in page]
    return JSONResponse({"result": result}, headers={"X-Total-Count": str(total)})


@app.get("/api/now/table/{tbl}/{sys_id}")
def get_one(tbl: str, sys_id: str, request: Request):
    auth_user(request)
    record = get_record(tbl, sys_id)
    if not record:
        raise HTTPException(status_code=404, detail="not found")
    mode, exclude = _flags(request)
    return {"result": render(record, tbl, _fields(request.query_params.get("sysparm_fields")),
                             mode, exclude, _base(request))}


@app.post("/api/now/table/{tbl}", status_code=201)
async def create_record(tbl: str, request: Request):
    user = auth_user(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    record = create(tbl, body, user)
    mode, exclude = _flags(request)
    return JSONResponse(status_code=201, content={"result": render(record, tbl, None, mode, exclude, _base(request))})


@app.api_route("/api/now/table/{tbl}/{sys_id}", methods=["PATCH", "PUT"])
async def update_record(tbl: str, sys_id: str, request: Request):
    user = auth_user(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    record = update(tbl, sys_id, body, user)
    mode, exclude = _flags(request)
    return {"result": render(record, tbl, None, mode, exclude, _base(request))}


@app.delete("/api/now/table/{tbl}/{sys_id}", status_code=204)
def remove_record(tbl: str, sys_id: str, request: Request):
    auth_user(request)
    if not delete_record(tbl, sys_id):
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(status_code=204, content=None)


# Mock-only: import records with their own sys_id, number and dates (used to
# load existing NEXUS tickets so they look like they always lived here).
@app.post("/mock/import/{tbl}")
async def import_records(tbl: str, request: Request):
    user = auth_user(request)
    body = await request.json()
    records = body.get("records", []) if isinstance(body, dict) else []
    created, skipped = 0, 0
    for item in records:
        if item.get("sys_id") and get_record(tbl, item["sys_id"]):
            skipped += 1
            continue
        create(tbl, dict(item), user, allow_system_fields=True)
        created += 1
    return {"created": created, "skipped": skipped}


@app.post("/oauth_token.do")
async def oauth_token(request: Request):
    """Same form fields and responses as ServiceNow's token endpoint."""
    TOKEN_REQUESTS["count"] += 1
    form = await request.form()
    grant = form.get("grant_type", "")
    if not CLIENT_SECRET or form.get("client_id") != CLIENT_ID or \
            not secrets.compare_digest(str(form.get("client_secret", "")), CLIENT_SECRET):
        return JSONResponse(status_code=401, content={"error_description": "access_denied", "error": "server_error"})
    if grant == "client_credentials":
        user = API_USER
    elif grant == "password":
        if not (secrets.compare_digest(str(form.get("username", "")), API_USER)
                and API_PASSWORD and secrets.compare_digest(str(form.get("password", "")), API_PASSWORD)):
            return JSONResponse(status_code=401, content={"error_description": "access_denied", "error": "server_error"})
        user = API_USER
    else:
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
    token = secrets.token_urlsafe(32)
    _tokens[token] = (user, time.time() + TOKEN_TTL)
    return {"access_token": token, "refresh_token": secrets.token_urlsafe(32), "scope": "useraccount",
            "token_type": "Bearer", "expires_in": TOKEN_TTL}


@app.post("/mock/revoke-tokens")
def revoke_tokens(request: Request):
    """Mock-only: drop every issued token (tests use it to simulate revocation)."""
    auth_user(request)
    _tokens.clear()
    return {"revoked": True}


@app.get("/mock/health")
def health():
    return {"status": "ok", "incidents": len(all_records("incident"))}


# ─────────────────────────────────────────────────────────────
#  Demo data
# ─────────────────────────────────────────────────────────────
def seed_demo() -> None:
    if all_records("sys_user"):
        return
    people = {
        "a1": ("Abel Tuter", "abel.tuter"), "a2": ("Beth Anglin", "beth.anglin"),
        "a3": ("Priya Raman", "priya.raman"), "a4": ("Rahul Mehta", "rahul.mehta"),
    }
    for key, (name, user_name) in people.items():
        save_record("sys_user", {"sys_id": (key * 16)[:32], "name": name, "user_name": user_name,
                                 "email": f"{user_name}@example.com"})
    for key, name in {"g1": "Service Desk", "g2": "Database Team", "g3": "Network Team"}.items():
        save_record("sys_user_group", {"sys_id": (key * 16)[:32], "name": name})
    if SEED != "demo":
        return
    samples = [
        ("Users cannot connect to VPN from the Pune office",
         "Since 9am, users in Pune get 'TLS handshake failed' when connecting to the corporate VPN. "
         "Bengaluru users are fine.", "network", "2", "1", "a3", "g3"),
        ("ORA-01555 snapshot too old during nightly batch",
         "The nightly GL reconciliation batch failed at 02:14 with ORA-01555: snapshot too old: "
         "rollback segment number 7 with name \"_SYSSMU7$\" too small.", "database", "2", "2", "a4", "g2"),
        ("Laptop keyboard not working after Windows update",
         "Several keys stopped responding after last night's update. External keyboard works.",
         "hardware", "3", "3", "a1", "g1"),
    ]
    for title, desc, cat, impact, urgency, caller, group in samples:
        create("incident", {"short_description": title, "description": desc, "category": cat,
                            "impact": impact, "urgency": urgency,
                            "caller_id": (caller * 16)[:32], "assignment_group": (group * 16)[:32]},
               "system")


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_demo()


# ─────────────────────────────────────────────────────────────
#  Web page (no login: this is a local test tool only)
# ─────────────────────────────────────────────────────────────
PAGE_CSS = """
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f4f5f7;color:#1d2433}
header{background:#1d2433;color:#fff;padding:14px 24px;display:flex;gap:16px;align-items:baseline}
header b{font-size:18px} header span{color:#b8c0d0;font-size:13px}
main{padding:20px 24px;max-width:1150px}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border-bottom:1px solid #e3e6eb;padding:8px 10px;text-align:left;font-size:13px;vertical-align:top}
th{background:#eef0f4;font-weight:bold}
a{color:#2952cc}
.card{background:#fff;border:1px solid #e3e6eb;border-radius:6px;padding:16px;margin:0 0 16px}
label{display:block;font-size:12px;color:#5b6475;margin:10px 0 4px}
input,select,textarea{width:100%;box-sizing:border-box;padding:7px;border:1px solid #c9cfd8;border-radius:4px;font:inherit}
button{background:#2952cc;color:#fff;border:0;border-radius:4px;padding:8px 14px;margin-top:12px;cursor:pointer}
.row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.note{border-left:3px solid #2952cc;background:#f7f8fb;padding:8px 12px;margin:8px 0;white-space:pre-wrap;font-size:13px}
.meta{color:#5b6475;font-size:12px}
"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{PAGE_CSS}</style></head><body>
<header><b>Mock ServiceNow</b><span>Local test instance for NEXUS AI · not affiliated with ServiceNow</span></header>
<main>{body}</main></body></html>""")


def options(choices: dict[str, str], selected: str = "") -> str:
    return "".join(f'<option value="{k}"{" selected" if k == selected else ""}>{html.escape(v)}</option>'
                   for k, v in choices.items())


def people_options(tbl: str, selected: str = "") -> str:
    rows = sorted(all_records(tbl), key=lambda r: r.get("name", ""))
    return '<option value=""></option>' + "".join(
        f'<option value="{r["sys_id"]}"{" selected" if r["sys_id"] == selected else ""}>{html.escape(r["name"])}</option>'
        for r in rows)


@app.get("/", response_class=HTMLResponse)
def ui_list():
    rows = sorted(all_records("incident"), key=lambda r: r["sys_updated_on"], reverse=True)
    body = "".join(
        f"<tr><td><a href='/ui/incident/{r['sys_id']}'>{html.escape(r.get('number', ''))}</a></td>"
        f"<td>{html.escape(r.get('short_description', ''))}</td>"
        f"<td>{html.escape(display_of('priority', r.get('priority', '')))}</td>"
        f"<td>{html.escape(display_of('state', r.get('state', '')))}</td>"
        f"<td>{html.escape(display_of('assignment_group', r.get('assignment_group', '')))}</td>"
        f"<td class='meta'>{r['sys_updated_on']} UTC</td></tr>" for r in rows)
    form = f"""<div class="card"><b>New incident</b>
<form method="post" action="/ui/incident">
<label>Short description</label><input name="short_description" required maxlength="160">
<label>Description</label><textarea name="description" rows="3"></textarea>
<div class="row">
<div><label>Category</label><select name="category">{options(CHOICES['category'], 'inquiry')}</select></div>
<div><label>Impact</label><select name="impact">{options(CHOICES['impact'], '3')}</select></div>
<div><label>Urgency</label><select name="urgency">{options(CHOICES['urgency'], '3')}</select></div>
<div><label>Caller</label><select name="caller_id">{people_options('sys_user')}</select></div>
</div>
<label>Assignment group</label><select name="assignment_group">{people_options('sys_user_group')}</select>
<button type="submit">Submit</button></form></div>"""
    table = (f"<table><tr><th>Number</th><th>Short description</th><th>Priority</th><th>State</th>"
             f"<th>Assignment group</th><th>Updated</th></tr>{body}</table>")
    return page("Incidents", form + f"<p class='meta'>{len(rows)} incidents</p>" + table)


@app.post("/ui/incident")
def ui_create(short_description: str = Form(...), description: str = Form(""), category: str = Form("inquiry"),
              impact: str = Form("3"), urgency: str = Form("3"), caller_id: str = Form(""),
              assignment_group: str = Form("")):
    record = create("incident", {"short_description": short_description, "description": description,
                                 "category": category, "impact": impact, "urgency": urgency,
                                 "caller_id": caller_id, "assignment_group": assignment_group}, "web.user")
    return RedirectResponse(f"/ui/incident/{record['sys_id']}", status_code=303)


@app.get("/nav_to.do", response_class=HTMLResponse)
def ui_nav(uri: str = ""):
    """Same link format as ServiceNow: /nav_to.do?uri=incident.do?sys_id=..."""
    match = re.search(r"sys_id=([0-9a-f]{32})", uri)
    if not match:
        raise HTTPException(status_code=404, detail="not found")
    return RedirectResponse(f"/ui/incident/{match.group(1)}", status_code=302)


@app.get("/ui/incident/{sys_id}", response_class=HTMLResponse)
def ui_detail(sys_id: str):
    r = get_record("incident", sys_id)
    if not r:
        return page("Not found", "<p>No such incident. <a href='/'>Back</a></p>")
    notes = "".join(
        f"<div class='note'><div class='meta'>{j['sys_created_on']} UTC · {html.escape(j['sys_created_by'])} · "
        f"{'Work note' if j['element'] == 'work_notes' else 'Comment'}</div>{html.escape(j['value'])}</div>"
        for j in journal_for(sys_id)) or "<p class='meta'>No notes yet.</p>"
    body = f"""<p><a href="/">← All incidents</a></p>
<div class="card"><b>{html.escape(r.get('number', ''))}</b> · {html.escape(r.get('short_description', ''))}
<p style="white-space:pre-wrap">{html.escape(r.get('description', ''))}</p>
<p class="meta">Caller: {html.escape(display_of('caller_id', r.get('caller_id', '')))} ·
Group: {html.escape(display_of('assignment_group', r.get('assignment_group', '')))} ·
Priority: {html.escape(display_of('priority', r.get('priority', '')))} · Updated {r['sys_updated_on']} UTC</p>
<form method="post" action="/ui/incident/{sys_id}">
<div class="row">
<div><label>State</label><select name="state">{options(CHOICES['state'], r.get('state', '1'))}</select></div>
<div><label>Impact</label><select name="impact">{options(CHOICES['impact'], r.get('impact', '3'))}</select></div>
<div><label>Urgency</label><select name="urgency">{options(CHOICES['urgency'], r.get('urgency', '3'))}</select></div>
<div><label>Category</label><select name="category">{options(CHOICES['category'], r.get('category', 'inquiry'))}</select></div>
</div>
<label>Add work note</label><textarea name="work_notes" rows="2"></textarea>
<button type="submit">Update</button></form></div>
<div class="card"><b>Activity</b>{notes}</div>"""
    return page(r.get("number", "Incident"), body)


@app.post("/ui/incident/{sys_id}")
def ui_update(sys_id: str, state: str = Form(...), impact: str = Form(...), urgency: str = Form(...),
              category: str = Form(...), work_notes: str = Form("")):
    body: dict[str, Any] = {"state": state, "impact": impact, "urgency": urgency, "category": category}
    if state in ("6", "7"):
        body.setdefault("close_code", "Solved (Permanently)")
    if work_notes.strip():
        body["work_notes"] = work_notes.strip()
    update("incident", sys_id, body, "web.user")
    return RedirectResponse(f"/ui/incident/{sys_id}", status_code=303)
