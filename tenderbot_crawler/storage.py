"""Durable, source-attributed records and resumable PDF jobs in SQLite."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlsplit


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def timestamp(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Source timestamps must include their timezone")
    return parsed


def official_url(value):
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and not url.username and not url.password
                and url.port in (None, 443) and
                (url.hostname == "gem.gov.in" or (url.hostname or "").endswith(".gem.gov.in")))
    except (ValueError, TypeError):
        return False


def validate_record(record):
    if not re.fullmatch(r"gem-\d{1,14}", record.get("id", "")):
        raise ValueError("Invalid GeM source ID")
    if not re.fullmatch(r"GEM/\d{4}/[BR]/\d+", record.get("tender_id", "")):
        raise ValueError("Invalid official GeM bid reference")
    if record.get("demo") or not official_url(record.get("source_document_url")):
        raise ValueError("A real official GeM source document is required")
    timestamp(record.get("source_fetched_at"))
    return record


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def collection_lock(root):
    """OS-owned lock is released after a crash; no stale PID guessing."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "collector.lock").open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another collector is using this data directory") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "tenderbot.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS tenders(
                id TEXT PRIMARY KEY, reference TEXT NOT NULL, record TEXT NOT NULL,
                source_fetched_at TEXT NOT NULL, first_seen_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tender_history(
                tender_id TEXT NOT NULL, hash TEXT NOT NULL, record TEXT NOT NULL,
                observed_at TEXT NOT NULL, PRIMARY KEY(tender_id, hash));
            CREATE VIRTUAL TABLE IF NOT EXISTS tender_search USING fts5(
                id UNINDEXED, reference, title, buyer, description);
            CREATE TABLE IF NOT EXISTS documents(
                id INTEGER PRIMARY KEY, tender_id TEXT NOT NULL REFERENCES tenders(id),
                url TEXT NOT NULL, name TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
                depth INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, fetched_at TEXT, sha256 TEXT, path TEXT,
                last_error TEXT, UNIQUE(tender_id, url));
            CREATE TABLE IF NOT EXISTS document_versions(
                document_id INTEGER NOT NULL REFERENCES documents(id), sha256 TEXT NOT NULL,
                path TEXT NOT NULL, fetched_at TEXT NOT NULL, bytes INTEGER NOT NULL,
                PRIMARY KEY(document_id, sha256));
            CREATE TABLE IF NOT EXISTS details(
                tender_id TEXT PRIMARY KEY REFERENCES tenders(id), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints(
                query TEXT PRIMARY KEY, page INTEGER NOT NULL, total INTEGER,
                fetched_at TEXT, complete INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                kind TEXT NOT NULL, status TEXT NOT NULL, summary TEXT);
        """)
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def upsert(self, record):
        validate_record(record)
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        semantic = {k: v for k, v in record.items() if k not in (
            "source_fetched_at", "other_details_fetched_at", "detail_warning")}
        digest = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()
        existing = self.get(record["id"])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tender_history VALUES(?,?,?,?)",
                            (record["id"], digest, encoded, record["source_fetched_at"]))
            if existing and timestamp(existing["source_fetched_at"]) > timestamp(record["source_fetched_at"]):
                return False
            self.db.execute("""INSERT INTO tenders VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                reference=excluded.reference,record=excluded.record,source_fetched_at=excluded.source_fetched_at""",
                (record["id"], record["tender_id"], encoded, record["source_fetched_at"], now()))
            self.db.execute("DELETE FROM tender_search WHERE id=?", (record["id"],))
            self.db.execute("INSERT INTO tender_search VALUES(?,?,?,?,?)", (
                record["id"], record["tender_id"], record.get("title", ""),
                " ".join(str(record.get(k) or "") for k in ("organisation", "department", "ministry")),
                record.get("description", "")))
        return True

    def get(self, tender_id):
        row = self.db.execute("SELECT record FROM tenders WHERE id=?", (tender_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def records(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT record FROM tenders ORDER BY source_fetched_at DESC,id")]

    def search(self, query, limit=20):
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            return self.records()[:limit]
        match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        rows = self.db.execute("""SELECT t.record FROM tender_search s JOIN tenders t ON t.id=s.id
            WHERE tender_search MATCH ? ORDER BY rank LIMIT ?""", (match, limit))
        return [json.loads(row[0]) for row in rows]

    def enqueue(self, tender_id, url, name, primary=False, depth=0, refresh=False):
        if not official_url(url):
            return False
        with self.db:
            self.db.execute("""INSERT INTO documents(tender_id,url,name,is_primary,depth) VALUES(?,?,?,?,?)
                ON CONFLICT(tender_id,url) DO UPDATE SET name=excluded.name,
                is_primary=MAX(documents.is_primary,excluded.is_primary),depth=MIN(documents.depth,excluded.depth)""",
                (tender_id, url, name or "GeM document", int(primary), depth))
            if refresh:
                self.db.execute("UPDATE documents SET status='pending' WHERE tender_id=? AND url=?", (tender_id, url))
        return True

    def jobs(self, refresh_before=None):
        if refresh_before:
            with self.db:
                self.db.execute("UPDATE documents SET status='pending' WHERE status='downloaded' AND fetched_at<?", (refresh_before,))
        return [dict(row) for row in self.db.execute("""SELECT * FROM documents
            WHERE status IN ('pending','error') ORDER BY is_primary DESC,id""")]

    def job_error(self, job_id, message):
        with self.db:
            self.db.execute("UPDATE documents SET status='error', attempts=attempts+1, last_error=? WHERE id=?",
                            (message[:400], job_id))

    def job_not_pdf(self, job_id, fetched_at=None):
        with self.db:
            self.db.execute("UPDATE documents SET status='not_pdf',attempts=attempts+1,fetched_at=?,last_error='Official link did not return a PDF' WHERE id=?", (fetched_at or now(), job_id))

    def reusable_document(self, job, since):
        row = self.db.execute("""SELECT * FROM documents WHERE url=? AND id<>? AND
            status IN ('downloaded','not_pdf') AND fetched_at>=? ORDER BY fetched_at DESC LIMIT 1""",
            (job["url"], job["id"], since)).fetchone()
        if row and (row["status"] == "not_pdf" or (self.root / row["path"]).is_file()):
            return dict(row)
        return None

    def save_document(self, job, content, fetched_at):
        if not content.startswith(b"%PDF-"):
            raise ValueError("Source returned a non-PDF document")
        timestamp(fetched_at)
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("pdfs") / (digest + ".pdf")
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_suffix(".part")
            temporary.write_bytes(content)
            os.replace(temporary, path)
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO document_versions VALUES(?,?,?,?,?)""",
                            (job["id"], digest, relative.as_posix(), fetched_at, len(content)))
            self.db.execute("""UPDATE documents SET status='downloaded',attempts=attempts+1,
                fetched_at=?,sha256=?,path=?,last_error=NULL WHERE id=?""",
                (fetched_at, digest, relative.as_posix(), job["id"]))
        return path

    def save_details(self, tender_id, details):
        if details.get("id") != tender_id or not official_url(details.get("source_document_url")):
            raise ValueError("Document evidence must belong to the requested official tender")
        timestamp(details.get("source_document_fetched_at"))
        record = self.get(tender_id)
        if record is None or (details.get("tender_id") and details["tender_id"] != record["tender_id"]):
            raise ValueError("Document bid reference differs from the listing")
        old = self.db.execute("SELECT data FROM details WHERE tender_id=?", (tender_id,)).fetchone()
        if old and timestamp(json.loads(old[0])["source_document_fetched_at"]) > timestamp(details["source_document_fetched_at"]):
            return
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO details VALUES(?,?)", (tender_id, json.dumps(details, ensure_ascii=False)))

    def begin_run(self, kind):
        with self.db:
            # A lock is held by the caller; any surviving running row is an interrupted process.
            self.db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (now(),))
            return self.db.execute("INSERT INTO runs(started_at,kind,status) VALUES(?,?,'running')", (now(), kind)).lastrowid

    def finish_run(self, run_id, status, summary):
        with self.db:
            self.db.execute("UPDATE runs SET finished_at=?,status=?,summary=? WHERE id=?",
                            (now(), status, json.dumps(summary), run_id))

    def checkpoint(self, query, page, total, fetched_at, complete=False):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)",
                            (query, page, total, fetched_at, int(complete)))

    def next_page(self, query):
        row = self.db.execute("SELECT page,complete FROM checkpoints WHERE query=?", (query,)).fetchone()
        return row[0] + 1 if row and not row[1] else 1

    def status(self):
        last = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        success = self.db.execute("SELECT finished_at FROM runs WHERE status='succeeded' AND kind='crawl' ORDER BY id DESC LIMIT 1").fetchone()
        counts = dict(self.db.execute("SELECT status,COUNT(*) FROM documents GROUP BY status"))
        sweep = self.db.execute("SELECT page,total,fetched_at,complete FROM checkpoints WHERE query='' ").fetchone()
        return {"status": last["status"] if last else "not_started",
                "last_run_at": last["started_at"] if last else None,
                "last_success_at": success[0] if success else None,
                "last_run_kind": last["kind"] if last else None,
                "records": self.db.execute("SELECT COUNT(*) FROM tenders").fetchone()[0],
                "documents": counts.get("downloaded", 0),
                "document_versions": self.db.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0],
                "unique_pdfs": self.db.execute("SELECT COUNT(DISTINCT sha256) FROM document_versions").fetchone()[0],
                "pending_documents": counts.get("pending", 0) + counts.get("error", 0),
                "errors": counts.get("error", 0),
                "unavailable_documents": counts.get("not_pdf", 0),
                "source_total": sweep['total'] if sweep else None,
                "pages_collected": sweep['page'] if sweep else 0,
                "full_sweep_complete": bool(sweep['complete']) if sweep else False,
                "last_run": json.loads(last["summary"]) if last and last["summary"] else None}

    def export(self, destination=None):
        folder = Path(destination) if destination else self.root / "export"
        records = self.records()
        stamps = [r["source_fetched_at"] for r in records]
        latest = max(stamps, key=timestamp) if stamps else None
        details = {row[0]: json.loads(row[1]) for row in self.db.execute("SELECT tender_id,data FROM details")}
        coverage = [dict(row) for row in self.db.execute("SELECT * FROM checkpoints")]
        snapshot = {"source": "GeM", "provider": "gem-playwright", "mode": "collected",
                    "source_url": "https://bidplus.gem.gov.in/all-bids", "fetched_at": latest,
                    "oldest_record_at": min(stamps, key=timestamp) if stamps else None,
                    "exported_at": now(), "total": len(records), "tenders": records,
                    "coverage": coverage,
                    "warning": "Collected public GeM records. Coverage is limited to completed searches and pages; check each record's retrieval date."}
        atomic_json(folder / "gem-details.json", details)
        atomic_json(folder / "real-gem.json", snapshot)
        atomic_json(folder / "collector-status.json", self.status())
        return folder
