"""Safe append-only data-review action ledger and projection."""
from __future__ import annotations
import hashlib, json, os, re, sqlite3
from datetime import datetime, timezone
from threading import Lock, RLock
from typing import Any
from services.operations.lifecycle import InstanceLock
from utils.paths import runtime_path

ACTIONS = {"confirm_suspension", "no_trade", "non_stock", "request_5m_repair", "pull_qmt", "pull_tdx", "keep_pending", "mark_reviewed"}
_HASH = re.compile(r"^[0-9a-fA-F]{64}$"); _LOCAL = Lock()
class ReviewError(ValueError): pass
class InvalidReview(ReviewError): pass
class ForbiddenReview(ReviewError): pass
class ConflictReview(ReviewError): pass
class BusyReview(ReviewError): pass

def db_path(): return runtime_path("operations", "data_review.sqlite3")
def _connect():
 p=db_path(); p.parent.mkdir(parents=True,exist_ok=True); cx=sqlite3.connect(p,timeout=10); cx.row_factory=sqlite3.Row; cx.execute("PRAGMA busy_timeout=10000")
 cx.executescript("""CREATE TABLE IF NOT EXISTS review_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,idempotency_key TEXT NOT NULL UNIQUE,review_key TEXT NOT NULL,action TEXT NOT NULL,fingerprint TEXT NOT NULL,contract_version TEXT NOT NULL,evidence_hash TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS review_projection(review_key TEXT PRIMARY KEY,action TEXT NOT NULL,fingerprint TEXT NOT NULL,contract_version TEXT NOT NULL,evidence_hash TEXT NOT NULL,payload_json TEXT NOT NULL,event_id INTEGER NOT NULL,updated_at TEXT NOT NULL);""")
 return cx

def _item_version(key,event_id,evidence): return hashlib.sha256(f"{key}|{event_id}|{evidence}".encode()).hexdigest()
def _queue_fingerprint(cx):
 rows=cx.execute("SELECT review_key,event_id,evidence_hash FROM review_projection ORDER BY review_key").fetchall()
 return hashlib.sha256(json.dumps([list(r) for r in rows],separators=(",",":")).encode()).hexdigest()
def _norm(p):
 action=str(p.get("action","")).strip(); key=str(p.get("review_key") or p.get("item_key") or "").strip(); idem=str(p.get("idempotency_key") or "").strip(); fp=str(p.get("fingerprint") or p.get("expected_queue_fingerprint") or "").strip(); ver=str(p.get("contract_version") or p.get("version") or "v1").strip(); expected_ver=str(p.get("expected_item_version") or "").strip(); eh=str(p.get("evidence_hash") or "").strip().lower(); token=str(p.get("confirm_token") or "").strip(); refs=p.get("evidence_refs") if isinstance(p.get("evidence_refs"),list) else []
 if action not in ACTIONS: raise InvalidReview("unsupported action")
 if not key or len(key)>300 or "\n" in key: raise InvalidReview("item_key/review_key is required")
 if not idem: raise InvalidReview("idempotency_key is required")
 if not fp: raise InvalidReview("expected_queue_fingerprint/fingerprint is required")
 if not _HASH.fullmatch(eh): raise InvalidReview("evidence_hash must be SHA-256")
 if not token: raise ForbiddenReview("confirm_token is required")
 operator = os.environ.get("AISTOCK_REVIEW_OPERATOR")
 if not operator: raise ForbiddenReview("server review operator is not configured")
 canonical = json.dumps({"reason": str(p.get("reason") or ""), "evidence_refs": refs}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
 if eh != hashlib.sha256(canonical).hexdigest(): raise InvalidReview("evidence_hash does not match canonical evidence_refs")
 expected_token = os.environ.get("AISTOCK_REVIEW_CONFIRM_TOKEN")
 # token is checked against the source fingerprint inside record_action
 actor = "api:" + os.environ.get("AISTOCK_REVIEW_OPERATOR", "unknown")
 return {"action":action,"review_key":key,"idempotency_key":idem,"fingerprint":fp,"expected_item_version":expected_ver,"contract_version":ver,"evidence_hash":eh,"evidence_refs":refs,"confirm_token":token,"reason":str(p.get("reason") or ""),"actor":actor,"payload":p}

def _request_digest(n):
 return hashlib.sha256(json.dumps({"action":n["action"],"fingerprint":n["fingerprint"],"contract_version":n["contract_version"],"evidence_hash":n["evidence_hash"],"reason":n["reason"],"evidence_refs":n["evidence_refs"],"payload":n["payload"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_action(p):
 n=_norm(p); request_digest=_request_digest(n)
 with _LOCAL:
  lock=InstanceLock(runtime_path("operations","data-review-actions.lock"))
  try: lock.acquire()
  except RuntimeError as e: raise BusyReview(str(e)) from e
  try:
   cx=_connect(); now=datetime.now(timezone.utc).isoformat(timespec="seconds")
   try:
    cx.execute("BEGIN IMMEDIATE"); qfp=_queue_fingerprint(cx); prior=cx.execute("SELECT * FROM review_events WHERE idempotency_key=?",(n["idempotency_key"],)).fetchone()
    if prior:
     prior_payload = json.loads(prior["payload_json"])
     if prior["review_key"]!=n["review_key"] or prior_payload.get("_request_digest") != request_digest: raise ConflictReview("idempotency key request digest conflict")
     cx.rollback(); return {"event_id":prior["event_id"],"idempotent_replay":True,"action_state":"recorded"}
    source_path, source_data, source_fp = source_snapshot()
    if not source_fp: raise ForbiddenReview("source queue snapshot is unavailable")
    source_item = next((x for x in source_data.get("items", []) if _source_item_key(x) == n["review_key"]), None)
    if not source_item: raise ConflictReview("source review item is unknown")
    if n["action"] not in allowed_actions_for_item(source_item): raise ConflictReview("action is not allowed for source item category")
    if source_fp and n["confirm_token"] != source_fp: raise ForbiddenReview("confirm_token must equal source queue fingerprint")
    if source_fp and n["fingerprint"] != source_fp: raise ConflictReview("source queue fingerprint is stale")
    if not source_fp and n["fingerprint"] != qfp: raise ConflictReview("queue fingerprint is stale")
    if source_fp and n["expected_item_version"]:
     source_item = next((x for x in source_data.get("items", []) if _source_item_key(x) == n["review_key"]), None)
     if source_item is None or source_item_version(source_item) != n["expected_item_version"]: raise ConflictReview("source item version is stale")
    prior_item=cx.execute("SELECT * FROM review_projection WHERE review_key=?",(n["review_key"],)).fetchone()
    # expected_item_version is the source version, checked above; projection versions belong to a different domain.
    before=dict(prior_item) if prior_item else None
    pull_job_id = None
    if n["action"] in {"pull_qmt", "pull_tdx", "request_5m_repair"}:
     # Persist a canonical, bounded repair job for the host executor. The API
     # container never imports QMT/TDX SDKs; run_ingestion_backlog.py executes
     # it in the configured host session and writes its verification report.
     from services.operations.ingestion_backlog import enqueue
     code = str(source_item.get("code") or "")
     day = str(source_item.get("date") or source_item.get("trade_date") or "")[:10]
     if not code or not day:
      raise InvalidReview("source review item lacks a canonical code/date")
     if n["action"] in {"pull_qmt", "request_5m_repair"}:
      pull_job_id = enqueue(["--mode", "minute-gap-repair", "--scenario", "manual", "--start-date", day, "--end-date", day, "--codes", code, "--minute-periods", "5m", "--max-repair-codes", "1", "--repair-workers", "1"], kind="ingestion")
     else:
      pull_job_id = "tdx_manual_" + hashlib.sha256(n["review_key"].encode()).hexdigest()[:24]
    cur=cx.execute("INSERT INTO review_events(idempotency_key,review_key,action,fingerprint,contract_version,evidence_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",(n["idempotency_key"],n["review_key"],n["action"],n["fingerprint"],n["contract_version"],n["evidence_hash"],json.dumps({**n["payload"],"_request_digest":request_digest,"actor":n["actor"],"reason":n["reason"],"evidence_refs":n["evidence_refs"],"before":before,"result":"recorded"},ensure_ascii=False,sort_keys=True),now)); eid=cur.lastrowid
    payload_with_job = {**n["payload"], "pull_job_id": pull_job_id, "pull_state": "queued" if pull_job_id else None}
    cx.execute("INSERT INTO review_projection(review_key,action,fingerprint,contract_version,evidence_hash,payload_json,event_id,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(review_key) DO UPDATE SET action=excluded.action,fingerprint=excluded.fingerprint,contract_version=excluded.contract_version,evidence_hash=excluded.evidence_hash,payload_json=excluded.payload_json,event_id=excluded.event_id,updated_at=excluded.updated_at",(n["review_key"],n["action"],n["fingerprint"],n["contract_version"],n["evidence_hash"],json.dumps(payload_with_job,ensure_ascii=False,sort_keys=True),eid,now))
    # A business exemption is durable input to the next audit.  It is keyed by
    # the exact security/date/period gap, so a later gap cannot be hidden.
    if n["action"] in {"no_trade", "confirm_suspension", "non_stock"}:
     override_path = runtime_path("minute_migration_v2", "manual_scope_overrides_20260913.json")
     override_path.parent.mkdir(parents=True, exist_ok=True)
     try: overrides = json.loads(override_path.read_text(encoding="utf-8")).get("overrides", []) if override_path.exists() else []
     except (OSError, ValueError): overrides = []
     code = str(source_item.get("code") or "")
     day = str(source_item.get("date") or source_item.get("trade_date") or "")[:10]
     if not code or not day:
      raise InvalidReview("source review item lacks a canonical code/date")
     overrides = [x for x in overrides if not (x.get("code") == code and x.get("trade_date") == day)]
     overrides.append({"code": code, "trade_date": day, "classification": n["action"], "reason": n["reason"], "event_id": eid, "updated_at": now})
     tmp = override_path.with_suffix(".tmp"); tmp.write_text(json.dumps({"overrides": overrides}, ensure_ascii=False, indent=2), encoding="utf-8"); tmp.replace(override_path)
    cx.commit(); return {"event_id":eid,"idempotent_replay":False,"action_state":"queued" if pull_job_id else "recorded","pull_job_id":pull_job_id,"detail":"已提交5分钟修复执行器，将依次尝试QMT与TDX并回写复核结果" if n["action"] == "request_5m_repair" else "已提交到QMT补数执行器，等待写后复核" if n["action"] == "pull_qmt" else "已登记TDX拉取请求，等待TDX执行器回传" if n["action"] == "pull_tdx" else None,"item_version":_item_version(n["review_key"],eid,n["evidence_hash"]),"queue_fingerprint":_queue_fingerprint(cx)}
   except: cx.rollback(); raise
   finally: cx.close()
  finally: lock.release()

def allowed_actions_for_item(item):
 issue_type = str(item.get("issue_type") or "").lower()
 if issue_type == "source_5m_incomplete_bars":
  # The purchased source itself is incomplete. Do not create synthetic bars.
  return {"keep_pending", "mark_reviewed"}
 if issue_type == "project_daily_missing_for_5m":
  return {"pull_qmt", "pull_tdx", "request_5m_repair", "keep_pending", "mark_reviewed"}
 category = str(item.get("category") or "").lower()
 if any(x in category for x in ("non_stock", "index", "bond", "etf")): return {"non_stock", "keep_pending", "mark_reviewed"}
 if "not_listed" in category or "listing:" in category: return {"keep_pending", "non_stock", "mark_reviewed"}
 if "manual_no_data" in category: return {"no_trade", "request_5m_repair", "keep_pending", "mark_reviewed"}
 if "suspend" in category or "no_trade" in category: return {"confirm_suspension", "no_trade", "keep_pending", "mark_reviewed"}
 if item.get("validity") == "blocked_evidence": return {"no_trade", "pull_qmt", "pull_tdx", "request_5m_repair", "keep_pending", "mark_reviewed"}
 return {"pull_qmt", "pull_tdx", "request_5m_repair", "keep_pending", "mark_reviewed"}


def _source_item_key(item):
 key = item.get("review_key") or item.get("item_key")
 if key:
  return str(key)
 code = item.get("code")
 day = item.get("date") or item.get("trade_date")
 return f"{code}|{day}"


def _source_text(value):
 if isinstance(value, bytes):
  return value.decode("utf-8", errors="replace").rstrip("\x00")
 return str(value)


def _purchased_history_snapshot():
 """Read the isolated purchased-history review table, if materialized."""
 manifest_path = runtime_path("purchased_history", "data_review_manifest.json")
 if not manifest_path.exists():
  return [], ""
 try:
  manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
  table = str(manifest.get("table") or "stock.purchased_history_data_review_items")
  run_id = str(manifest.get("run_id") or "")
  if not run_id or not re.fullmatch(r"stock\.[A-Za-z0-9_]+", table):
   return [], ""
  from utils.market_warehouse import clickhouse_client
  rows = clickhouse_client().query(
   f"""
   SELECT review_key, issue_type, source, code, trade_date, period,
          security_type, expected_count, actual_count, missing_count,
          source_batch_id, source_archive, source_entry, evidence_ref,
          evidence_sha256, reason
   FROM {table}
   WHERE run_id = %(run_id)s
   ORDER BY trade_date, code, issue_type
   """,
   parameters={"run_id": run_id},
  ).result_rows
 except Exception:
  return [], ""

 items = []
 for row in rows:
  (review_key, issue_type, source, code, trade_date, period,
   security_type, expected_count, actual_count, missing_count,
   source_batch_id, source_archive, source_entry, evidence_ref,
   evidence_sha256, reason) = row
  items.append({
   "review_key": str(review_key),
   "issue_type": str(issue_type),
   "source": str(source),
   "code": str(code),
   "date": str(trade_date)[:10],
   "period": str(period),
   "security_type": str(security_type),
   "expected_count": int(expected_count),
   "actual_count": int(actual_count),
   "missing_count": int(missing_count),
   "source_batch_id": str(source_batch_id),
   "source_archive": str(source_archive),
   "source_entry": str(source_entry),
   "evidence_ref": str(evidence_ref),
   "evidence_sha256": _source_text(evidence_sha256),
   "reason": str(reason or ""),
   "category": f"purchased_history:{issue_type}",
   "validity": "blocked",
  })
 fingerprint = hashlib.sha256(json.dumps(
  {"manifest": manifest, "items": items},
  ensure_ascii=False, sort_keys=True, separators=(",", ":")
 ).encode("utf-8")).hexdigest()
 return items, fingerprint


_SNAPSHOT_CACHE: dict[str, Any] = {}
_SNAPSHOT_CACHE_TTL_SECONDS = 60.0
_SNAPSHOT_CACHE_LOCK = RLock()


def _snapshot_cache_key() -> tuple:
  manifest_path = runtime_path("purchased_history", "data_review_manifest.json")
  root = runtime_path("minute_migration_v2")
  try:
    mtime = manifest_path.stat().st_mtime if manifest_path.exists() else 0.0
  except OSError:
    mtime = 0.0
  try:
    newest = max((p.stat().st_mtime for p in root.glob("scoped_anomalies_*.json")), default=0.0)
  except OSError:
    newest = 0.0
  return (str(manifest_path), mtime, newest)


def source_snapshot():
  key = _snapshot_cache_key()
  now = datetime.now(timezone.utc).timestamp()
  with _SNAPSHOT_CACHE_LOCK:
    cached = _SNAPSHOT_CACHE.get("value")
    cached_key = _SNAPSHOT_CACHE.get("key")
    cached_at = float(_SNAPSHOT_CACHE.get("at") or 0.0)
  if cached is not None and cached_key == key and now - cached_at < _SNAPSHOT_CACHE_TTL_SECONDS:
    return cached
  result = _load_source_snapshot()
  with _SNAPSHOT_CACHE_LOCK:
    _SNAPSHOT_CACHE["key"] = key
    _SNAPSHOT_CACHE["at"] = now
    _SNAPSHOT_CACHE["value"] = result
  return result


def _load_source_snapshot():
 root = runtime_path("minute_migration_v2")
 paths = sorted(root.glob("scoped_anomalies_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
 purchased_items, purchased_fp = _purchased_history_snapshot()
 if not paths and not purchased_items: return None, {}, ""
 if paths:
  path = paths[0]; raw = path.read_bytes(); data = json.loads(raw.decode("utf-8")); base_fp = hashlib.sha256(raw).hexdigest()
 else:
  path = runtime_path("purchased_history", "data_review_manifest.json"); data = {}; base_fp = ""
 if purchased_items:
  data = {**data, "items": list(data.get("items") or []) + purchased_items}
  fingerprint = hashlib.sha256(json.dumps(
   {"base": base_fp, "purchased": purchased_fp}, sort_keys=True, separators=(",", ":")
  ).encode("utf-8")).hexdigest()
 else:
  fingerprint = base_fp
 return path, data, fingerprint


def source_item_version(item):
 return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def current_queue_fingerprint():
 _path, _data, digest = source_snapshot()
 return digest or ""


def reviewed_keys():
 cx = _connect()
 try:
  rows = cx.execute("SELECT DISTINCT review_key FROM review_events WHERE action=?", ("mark_reviewed",)).fetchall()
  return {str(row[0]) for row in rows}
 finally:
  cx.close()


def queue(limit=200):
 cx=_connect()
 try:
  marked_reviewed = reviewed_keys()
  out=[]
  for r in cx.execute("SELECT * FROM review_projection ORDER BY updated_at DESC LIMIT ?",(max(1,min(int(limit),1000)),)).fetchall():
   d=dict(r); payload=json.loads(d.pop("payload_json")); d["payload"]=payload
   if payload.get("pull_job_id") and d["action"] == "pull_qmt":
    try:
     from services.operations.ingestion_backlog import connect
     job = connect().execute("SELECT state,error FROM jobs WHERE id=?", (payload["pull_job_id"],)).fetchone()
     if job: payload["pull_state"], payload["pull_error"] = job[0], job[1]
    except Exception as exc: payload["pull_state"], payload["pull_error"] = "unknown", str(exc)
   d["item_version"]=_item_version(d["review_key"],d["event_id"],d["evidence_hash"]); d["evidence_fingerprint"]=d["evidence_hash"]; d["allowed_actions"]=sorted(ACTIONS); d["action_state"]=payload.get("pull_state") or "recorded"; d["blocking_impact"]="repair_queue_only" if d["action"] in {"request_5m_repair","pull_qmt","pull_tdx"} else "audit_ledger_only"; d["requires_evidence"]=True; d["reviewed"] = d["review_key"] in marked_reviewed; d["review_status"] = "reviewed" if d["reviewed"] else "unreviewed"; out.append(d)
  return out
 finally: cx.close()
