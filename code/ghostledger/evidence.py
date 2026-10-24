"""Parse every evidence file once into typed observations with provenance.

An Observation is one sighting of one resource in one evidence file. It carries:
  provenance  - file_id, file capture time, the record-level observation time and the location
                (SQL table / JSON collection / log file) where the sighting was made;
  identity    - resource_id, system_id and normalized identifiers (emails, phone digits,
                sha256 hashes, user ids) found on the record or on its trace's lookup lines;
  semantics   - a data category hint and a normalized state (PRESENT, ABSENT, IDENTIFIABLE,
                ANONYMIZED, REDACTED, ACTIVE, DISABLED, BLOCKED) and whether the state was
                directly observed or is only a reported claim.
Files also carry Scope records: which systems/collections a file enumerates completely, so that
"not listed" can be read as "absent" only where the file actually covers that system.
"""
import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

PREFIX_SYSTEM = {
    "account": "account_service", "search": "search_service", "analytics": "analytics_platform",
    "support": "support_platform", "export": "export_service", "data_flow": "integration_service",
    "backup": "backup_service", "audit_record": "security_service", "tombstone": "identity_service",
    "legal_hold": "legal_service", "email_delivery": "email_delivery_service", "cache": "cache_service",
    "marketing": "marketing_platform", "object": "object_storage",
}
RESOURCE_RE = re.compile(r"\b(" + "|".join(sorted(PREFIX_SYSTEM, key=len, reverse=True)) + r")_\d+\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b")
PHONE_RE = re.compile(r"^\+?[\d][\d\s().-]{6,}\d$")

# Inventory SQL tables and JSON collections -> system they enumerate.
TABLE_SYSTEM = {
    "customer_profiles": "account_service", "customer_profiles_primary": "account_service",
    "customer_profiles_failover_replica": "account_service",
    "customer_search_documents": "search_service", "search_documents_primary": "search_service",
    "customer_analytics_profiles": "analytics_platform", "customer_analytics_reports": "analytics_platform",
    "support_records": "support_platform", "support_conversations": "support_platform",
    "ticket_attachments": "support_platform", "customer_exports": "export_service",
    "customer_export_jobs": "export_service", "connector_subscriptions": "integration_service",
    "connector_configurations": "integration_service", "backup_restore_entries": "backup_service",
    "backup_catalog": "backup_service", "security_audit_records": "security_service",
    "security_audit_events": "security_service", "deleted_customer_references": "identity_service",
    "retained_order_records": "legal_service",
}
# Keys whose values are never identity evidence.
NON_IDENTITY_KEYS = {"resource_id", "integrity_seal", "trace_id", "source_resource_id", "seq", "_ts"}
# Keys a trace's sibling lines may contribute to the line that names the resource.
TRACE_SUBJECT_KEYS = {"lookup_key", "match_key", "subject_key", "subject_ref", "owner_key", "correlation_key",
                      "scan_key", "customer_reference", "email", "phone", "source"}

STATES = {"PRESENT", "ABSENT", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED", "BLOCKED"}


def system_of(resource_id):
    m = RESOURCE_RE.fullmatch(resource_id or "")
    return PREFIX_SYSTEM.get(m.group(1)) if m else None


def prefix_of(resource_id):
    m = RESOURCE_RE.fullmatch(resource_id or "")
    return m.group(1) if m else None


@dataclass
class Observation:
    file_id: str
    file_captured_at: str
    observed_at: str
    resource_id: str
    system_id: str
    location: str
    kind: str                     # sql_table | sql_replica | snapshot | history | service_export | record_list | log | handover | document
    state: str = None             # normalized state, see STATES
    category: str = None          # data-category hint (rule data_category or sub-type)
    claim: bool = False           # state is a reported result rather than a direct read of the record
    identifiers: frozenset = frozenset()
    links: tuple = ()             # explicit (resource_id) links carried on the record, e.g. source_resource_id
    attrs: dict = field(default_factory=dict)


@dataclass
class Scope:
    file_id: str
    file_captured_at: str
    system_id: str
    location: str
    captured_at: str
    resources: frozenset          # resources listed for that system in this location


# ---------------------------------------------------------------------------------------------
# identifier normalization

def norm_phone(s):
    return re.sub(r"\D", "", s)


def identifiers_in(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            if k not in NON_IDENTITY_KEYS:
                identifiers_in(v, out)
    elif isinstance(value, list):
        for v in value:
            identifiers_in(v, out)
    elif isinstance(value, str):
        for e in EMAIL_RE.findall(value):
            out.add("email:" + e.lower())
        for h in HASH_RE.findall(value.lower()):
            out.add("hash:" + h)
        for u in USER_RE.findall(value):
            out.add("user:" + u)
        if PHONE_RE.match(value.strip()) and len(norm_phone(value)) >= 8:
            out.add("phone:" + norm_phone(value))
    return out


# ---------------------------------------------------------------------------------------------
# state normalization per resource type

def _truthy(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "allow", "allowed"):
        return True
    if s in ("0", "false", "no", "deny", "denied"):
        return False
    return None


def _any_nonnull(a, keys):
    return any(a.get(k) not in (None, "", "null", "None") for k in keys)


RESULT_STATE = {"FOUND": "PRESENT", "NOT_FOUND": "ABSENT", "IDENTIFIABLE": "IDENTIFIABLE",
                "ANONYMIZED": "ANONYMIZED", "REDACTED": "REDACTED", "ACTIVE": "ACTIVE",
                "BLOCKED": "BLOCKED", "DISABLED": "DISABLED"}


def normalize(prefix, kind, location, a):
    """Return (state, category) for one record. category refines the data category where the
    record type alone is not enough (e.g. support attachment vs. message)."""
    rt = a.get("record_type")
    if rt and a.get("result") in RESULT_STATE and kind in ("handover", "record_list", "history", "log"):
        cat = rt
        return RESULT_STATE[a["result"]], cat
    ev = a.get("event", "") or ""
    if prefix == "account":
        if "status_code" in a:
            return ("ABSENT" if str(a["status_code"]) == "404" or a.get("profile") is None else "PRESENT"), "USER_PROFILE"
        if "http_status" in a:
            return ("ABSENT" if str(a["http_status"]) == "404" or str(a.get("rows")) == "0" else "PRESENT"), "USER_PROFILE"
        st = (a.get("account_status") or "").upper()
        return ("ABSENT" if st == "DELETED" else "PRESENT"), "USER_PROFILE"
    if prefix == "search":
        if "hits" in a:
            h = a["hits"]
            total = h.get("total") if isinstance(h, dict) else h
            return ("PRESENT" if total and int(total) > 0 else "ABSENT"), "SEARCH_INDEX_ENTRY"
        if "total_hits" in a:
            return ("PRESENT" if int(a["total_hits"] or 0) > 0 else "ABSENT"), "SEARCH_INDEX_ENTRY"
        if a.get("result") in ("hit", "miss") and ev.startswith("index_query"):
            return ("PRESENT" if a["result"] == "hit" else "ABSENT"), "SEARCH_INDEX_ENTRY"
        return "PRESENT", "SEARCH_INDEX_ENTRY"
    if prefix == "analytics":
        if ev == "contact_lookup_completed" or "email_opt_in" in a:
            return ("PRESENT" if str(a.get("rows", "1")) != "0" else "ABSENT"), "MARKETING_PROFILE"
        if "direct_identifiers" in a:
            return ("IDENTIFIABLE" if str(a["direct_identifiers"]) != "0" else "ANONYMIZED"), "ANALYTICS_PROFILE"
        keys = ("user_id", "email", "phone", "email_hash", "customer_reference")
        return ("IDENTIFIABLE" if _any_nonnull(a, keys) else "ANONYMIZED"), "ANALYTICS_PROFILE"
    if prefix == "support":
        if ev in ("contact_query_completed", "deletion_marker_check") or location == "support_contact_index":
            return ("PRESENT" if str(a.get("rows", "1")) != "0" else "ABSENT"), "SUPPORT_ATTACHMENT"
        if ev == "attachment_pii_scan" or location == "ticket_attachments" or "file_name" in a and "message_text" not in a:
            return "PRESENT", "SUPPORT_ATTACHMENT"
        if ev == "message_pii_scan":
            return "IDENTIFIABLE", "SUPPORT_MESSAGE"
        if "message_text" in a:
            txt = a.get("message_text") or ""
            return ("REDACTED" if txt.strip() == "[REDACTED]" or not identifiers_in(txt) and not _any_nonnull(a, ("requester_email", "requester_name", "requester_phone")) else "IDENTIFIABLE"), "SUPPORT_MESSAGE"
        if any(k in a for k in ("requester_email", "requester_name", "requester_phone")):
            return ("IDENTIFIABLE" if _any_nonnull(a, ("requester_email", "requester_name", "requester_phone")) else "REDACTED"), "SUPPORT_MESSAGE"
        if kind == "document":
            return "PRESENT", "SUPPORT_ATTACHMENT"
        return "PRESENT", "SUPPORT_MESSAGE"
    if prefix == "export":
        av = _truthy(a.get("available", a.get("download_available")))
        return ("ABSENT" if av is False and kind == "log" else "PRESENT"), "USER_EXPORT"
    if prefix == "data_flow":
        en = _truthy(a.get("enabled"))
        if en is None:
            return None, "DATA_FLOW"
        return ("ACTIVE" if en else "DISABLED"), "DATA_FLOW"
    if prefix == "backup":
        if "decision" in a:
            return ("BLOCKED" if _truthy(a["decision"]) is False else "ACTIVE"), "BACKUP_ARCHIVE"
        if "eligible" in a:
            ok = _truthy(a["eligible"]) and _truthy(a.get("deletion_block")) is not True
            return ("ACTIVE" if ok else "BLOCKED"), "BACKUP_ARCHIVE"
        ra = _truthy(a.get("restore_allowed"))
        if ra is None:
            return None, "BACKUP_ARCHIVE"
        return ("ACTIVE" if ra else "BLOCKED"), "BACKUP_ARCHIVE"
    if prefix == "audit_record":
        return "PRESENT", "SECURITY_AUDIT_LOG"
    if prefix == "tombstone":
        return ("IDENTIFIABLE" if _any_nonnull(a, ("name", "email", "phone", "email_hash")) else "REDACTED"), "ACCOUNT_TOMBSTONE"
    if prefix == "legal_hold":
        return "PRESENT", "LEGAL_HOLD_RECORD"
    if prefix == "email_delivery":
        return ("IDENTIFIABLE" if identifiers_in(a) else "REDACTED"), "EMAIL_DELIVERY_LOG"
    if prefix == "cache":
        return "PRESENT", "CACHE_ENTRY"
    if prefix == "marketing":
        return "PRESENT", "MARKETING_PROFILE"
    return "PRESENT", None


# ---------------------------------------------------------------------------------------------
# parsers

class EvidenceIndex:
    def __init__(self, dataset):
        self.ds = dataset
        self.observations = []
        self.scopes = []
        self.file_status = {}       # file_id -> parsed | unparsed:<reason> | error:<msg>
        self.by_resource = defaultdict(list)

    # -- helpers
    def _add(self, fid, rid, observed_at, attrs, kind, location, system=None, extra_ids=None, links=()):
        if not rid or not RESOURCE_RE.fullmatch(rid):
            return
        prefix = prefix_of(rid)
        state, category = normalize(prefix, kind, location, attrs)
        ids = identifiers_in(attrs)
        if extra_ids:
            ids |= set(extra_ids)
        claim = kind in ("handover", "record_list", "history") and "record_type" in attrs
        o = Observation(
            file_id=fid, file_captured_at=self.ds.manifest[fid]["captured_at"],
            observed_at=observed_at or self.ds.manifest[fid]["captured_at"], resource_id=rid,
            system_id=system or system_of(rid), location=location, kind=kind, state=state,
            category=category, claim=claim, identifiers=frozenset(ids), links=tuple(links), attrs=attrs)
        self.observations.append(o)
        self.by_resource[rid].append(o)

    def _scope(self, fid, system, location, captured_at, resources):
        self.scopes.append(Scope(fid, self.ds.manifest[fid]["captured_at"], system, location,
                                 captured_at or self.ds.manifest[fid]["captured_at"], frozenset(resources)))

    # -- SQL
    def parse_sql(self, fid, path):
        con = sqlite3.connect(":memory:")
        with open(path, encoding="utf-8") as f:
            con.executescript(f.read())
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        scope = {}
        if "snapshot_scope" in tables:
            scope = dict(con.execute("select origin_system, captured_at from snapshot_scope"))
        listed = defaultdict(set)
        for t in tables:
            if t == "snapshot_scope":
                continue
            cur = con.execute(f'select * from "{t}"')
            cols = [d[0] for d in cur.description]
            for row in cur:
                d = dict(zip(cols, row))
                if "record_json" in d:
                    rec = json.loads(d["record_json"])
                    sysid = d.get("origin_system") or system_of(d["resource_id"])
                    self._add(fid, d["resource_id"], d.get("updated_at"), rec, "sql_replica", t, sysid)
                    listed[(sysid, t)].add(d["resource_id"])
                else:
                    sysid = TABLE_SYSTEM.get(t) or system_of(d["resource_id"])
                    links = [d["source_resource_id"]] if d.get("source_resource_id") else []
                    self._add(fid, d["resource_id"], d.get("updated_at") or d.get("indexed_at"), d, "sql_table", t,
                              sysid, links=links)
                    listed[(sysid, t)].add(d["resource_id"])
        if scope:
            # Inventory capture: snapshot_scope names the systems it enumerates completely.
            for sysid, cap in scope.items():
                res = set().union(*[v for (s, _), v in listed.items() if s == sysid]) if listed else set()
                self._scope(fid, sysid, "snapshot_scope", cap, res)
        else:
            # Table dumps without an explicit scope enumerate the systems of their typed tables.
            for (sysid, t), res in listed.items():
                if t in TABLE_SYSTEM:
                    self._scope(fid, sysid, t, None, res)
            # Replica captures enumerate, per replica table, every system they hold rows for.
            by_table = defaultdict(set)
            for (sysid, t), res in listed.items():
                if t not in TABLE_SYSTEM:
                    by_table[sysid] |= res
            for sysid, res in by_table.items():
                self._scope(fid, sysid, "replica_tables", None, res)

    # -- JSON
    def parse_json(self, fid, path):
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        if "snapshots" in j:
            for s in j["snapshots"]:
                coll = s["collection"]
                if coll == "operational_record_history":
                    for rec in s["records"]:
                        self._add(fid, rec["resource_id"], rec.get("recorded_at") or s["captured_at"], rec, "history",
                                  coll, rec.get("system"))
                    continue
                sysid = TABLE_SYSTEM.get(coll) or s.get("data_store")
                for rec in s["records"]:
                    self._add(fid, rec["resource_id"], s["captured_at"], rec, "snapshot", coll, sysid)
                self._scope(fid, sysid, coll, s["captured_at"], [r["resource_id"] for r in s["records"]])
        elif "service_exports" in j:
            for sysid, colls in j["service_exports"].items():
                for coll, recs in colls.items():
                    for rec in recs:
                        self._add(fid, rec["resource_id"], rec.get("observed_at"), rec, "service_export", coll, sysid)
        elif "records" in j:
            coll = j.get("collection") or "records"
            for rec in j["records"]:
                self._add(fid, rec["resource_id"], rec.get("recorded_at") or j.get("captured_at"), rec, "record_list",
                          coll, rec.get("system") or TABLE_SYSTEM.get(coll))
        else:
            raise ValueError("unknown JSON layout")

    # -- logs
    KV = re.compile(r'([A-Za-z_]+)=("(?:[^"\\]|\\.)*"|\S+)')

    @classmethod
    def kvs(cls, s):
        out = {}
        for k, v in cls.KV.findall(s):
            if v.startswith('"'):
                v = v[1:-1].replace('\\"', '"')
            out[k] = v
        return out

    @classmethod
    def parse_log_line(cls, line):
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(.*)$", line)
        if not m:
            return None
        ts, rest = m.groups()
        if rest.startswith("{"):
            d = json.loads(rest)
        elif rest.startswith("["):
            m2 = re.match(r"\[(\w+)\] \[([\w]+)\] \[([\w-]+)\] (\w+): ?(.*)", rest)
            d = cls.kvs(m2.group(5))
            d.update(level=m2.group(1), service=m2.group(2), trace_id=m2.group(3), event=m2.group(4))
        elif rest.startswith("| ") or " | " in rest:
            p = [x.strip() for x in rest.lstrip("| ").split(" | ", 5)]
            d = cls.kvs(p[-1])
            d.update(level=p[0], service=p[1], trace_id=p[2], event=p[3])
        elif rest.startswith("<"):
            m2 = re.match(r"<\d+>\d+ (\S+) (\S+) (\S+) - - (.*)", rest)
            d = cls.kvs(m2.group(4))
            d.update(service=m2.group(2), trace_id=m2.group(3))
        elif rest.startswith("requestId="):
            top = cls.kvs(rest)
            d = cls.kvs(top.get("message", ""))
            d.update(trace_id=top.get("requestId"), service=top.get("function"), level=top.get("level"))
        else:
            d = cls.kvs(rest)
        d["_ts"] = ts
        return d

    def parse_log(self, fid, path):
        with open(path, encoding="utf-8") as f:
            lines = [self.parse_log_line(l.rstrip("\n")) for l in f]
        lines = [l for l in lines if l]
        # A trace's *_started line carries the lookup key; its *_completed line carries the
        # resource state. Only subject keys travel between lines of one trace (never results,
        # so a cache "miss" on a sibling line cannot leak into the resource's state).
        subject = defaultdict(dict)
        for l in lines:
            t = l.get("trace_id")
            if t:
                for k, v in l.items():
                    if k in TRACE_SUBJECT_KEYS and v not in (None, "", "null"):
                        subject[t].setdefault(k, v)
        for l in lines:
            if "resource_id" not in l:
                continue
            merged = dict(subject.get(l.get("trace_id"), {})) if l.get("trace_id") else {}
            merged.update(l)
            self._add(fid, l["resource_id"], l["_ts"], merged, "log", os.path.basename(path), l.get("system"))
            if str(l.get("event", "")).endswith("_started"):
                self.observations[-1].state = None   # identity-only sighting; state comes from completion

    # -- handover text
    def parse_txt(self, fid, path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("- ") and "|" in line:
                    p = [x.strip() for x in line[2:].split("|")]
                    if len(p) >= 6:
                        self._add(fid, p[2], p[0], dict(system=p[1], record_type=p[3], result=p[4],
                                                        subject_reference=p[5]), "handover", "handover", p[1])

    # -- PDF documents: one observation per resource id mentioned; identifiers from the same
    # line block (row) when the document lists several subjects, else from the whole document.
    def parse_pdf(self, fid, path):
        try:
            import pypdf
        except ImportError:
            self.file_status[fid] = "unparsed:pypdf missing"
            return
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(path).pages)
        rids = sorted(set(m.group(0) for m in RESOURCE_RE.finditer(text)))
        doc_ids = set()
        for line in text.splitlines():
            identifiers_in(line.strip(), doc_ids)
        users = {i for i in doc_ids if i.startswith("user:")}
        for rid in rids:
            attrs = {"document_text": text}
            if len(users) > 1:
                rows = [l for l in text.splitlines() if rid in l]
                ids = set()
                for r in rows:
                    identifiers_in(r, ids)
            else:
                ids = set(doc_ids)
            retain = re.search(r"retain until (\d\d \w{3} \d{4})", text)
            if retain:
                attrs["retain_until_text"] = retain.group(1)
            self._add(fid, rid, None, attrs, "document", "pdf", extra_ids=ids)
        self.extra_documents = getattr(self, "extra_documents", {})
        self.extra_documents[fid] = text

    def build(self):
        handlers = {"sql": self.parse_sql, "json": self.parse_json, "log": self.parse_log,
                    "txt": self.parse_txt, "pdf": self.parse_pdf}
        for fid, m in sorted(self.ds.manifest.items()):
            path = self.ds.path(m["file_path"])
            ext = path.rsplit(".", 1)[-1].lower()
            h = handlers.get(ext)
            if not h:
                self.file_status[fid] = f"unparsed:{ext} (no OCR backend)"
                continue
            try:
                h(fid, path)
                self.file_status.setdefault(fid, "parsed")
            except Exception as e:  # keep going; report the gap
                self.file_status[fid] = f"error:{e!r}"
        return self
