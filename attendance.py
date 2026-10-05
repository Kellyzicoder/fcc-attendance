"""FCC Attendance Tracker: live check-in, live arrivals, follow-up, insights, member register and SQL.

Storage is a plain SQL database, so you can query it directly:
  • Postgres (Supabase or Neon) when `database_url` is set in Streamlit secrets — the real data;
  • otherwise a local SQLite demo database filled with invented names (reset whenever the app restarts).
Real member data is never stored in this repository.

Tables (same in SQLite and Postgres):
  members(id, full_name, phone, email, group_name, role, status, type, date_joined, first_visit,
          invited_by, follow_up, created_at, pastor, age_group, church, version)
  services(service_date PRIMARY KEY, name)
  attendance(service_date, member_id, checked_at, PRIMARY KEY (service_date, member_id))
  activity_log(id, at, kind, service_date, member_id, detail, by_name, result)   -- append-only history

Safe when several people use it at once (the same ideas banks use):
  • ticks are "make this person present / absent" requests, so repeating one changes nothing;
  • an untick only goes through if the tick is still the one that usher saw (optimistic check);
  • approving a sign-up is one all-or-nothing transaction that only the first admin can win;
  • each member row has a version number, so a stale edit is refused instead of overwriting someone else;
  • every change is written to activity_log, which is only ever added to.
"""
from __future__ import annotations

import datetime as dt
import re
from contextlib import contextmanager
import threading
import uuid
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

TZ = ZoneInfo("Pacific/Auckland")
BRAND = dict(navy="#2c4b77", teal="#208088", green="#2aa686", slate="#293641", gold="#ffcf00")  # from the FCC logo
YELLOW_AT, RED_AT = 3, 5          # services missed in a row
INACTIVE = {"inactive", "moved", "left", "deceased", "transferred", "away"}
ARCHIVE_DAYS = 730  # not seen for two years → moved to the Archive list (they come back out the day they're ticked)
AMBER, CRIMSON = "#fab219", "#d03b3b"   # reserved status colours (always shown with icon + label)
# Chart colours, validated for colour-blind separation and contrast on the dark surface (#141c22).
SERIES = dict(members="#2aa686", first_timers="#5a8ef0")
BLUE = "#5a8ef0"  # "missed this service": information, not a warning
STATUS = dict(ok="#2aa686", blue=BLUE, yellow=AMBER, red=CRIMSON)
INK = dict(primary="#e8eef2", secondary="#9fb0bd", muted="#6b7c89", grid="rgba(255,255,255,0.06)")


def today() -> dt.date:
    return dt.datetime.now(TZ).date()


def now_iso() -> str:
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def norm(name: str) -> str:
    return " ".join(str(name).split()).lower()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- storage (SQL: SQLite demo or Postgres)
MEMBER_COLS = ["full_name", "phone", "email", "group_name", "role", "status", "type", "date_joined", "first_visit",
               "invited_by", "follow_up", "created_at", "pastor", "age_group", "church"]
OPTIONAL_COLS = {"pastor": "has_pastor", "age_group": "has_age", "church": "has_church"}  # columns added later: used only once they exist
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS members (
        id TEXT PRIMARY KEY, full_name TEXT NOT NULL, phone TEXT, email TEXT, group_name TEXT, role TEXT,
        status TEXT, type TEXT DEFAULT 'member', date_joined DATE, first_visit DATE, invited_by TEXT,
        follow_up TEXT, created_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS services (service_date DATE PRIMARY KEY, name TEXT)",
    """CREATE TABLE IF NOT EXISTS attendance (
        service_date DATE NOT NULL REFERENCES services(service_date) ON DELETE CASCADE,
        member_id TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
        checked_at TEXT, PRIMARY KEY (service_date, member_id))""",
    "CREATE INDEX IF NOT EXISTS attendance_member ON attendance(member_id)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS email_log (
        id TEXT PRIMARY KEY, kind TEXT, report_date DATE, sent_at TEXT, recipients TEXT, ok BOOLEAN, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS activity_log (
        id TEXT PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, service_date DATE, member_id TEXT,
        detail TEXT, by_name TEXT, result TEXT)""",
    "CREATE INDEX IF NOT EXISTS activity_log_at ON activity_log(at)",
]
# Columns added after launch. Postgres skips ones that exist; SQLite reports "duplicate column", which is ignored.
MIGRATIONS = [("versioned", "ALTER TABLE members ADD COLUMN {ine}version INTEGER NOT NULL DEFAULT 1"),
              ("has_pastor", "ALTER TABLE members ADD COLUMN {ine}pastor TEXT"),
              ("has_age", "ALTER TABLE members ADD COLUMN {ine}age_group TEXT"),
              ("has_church", "ALTER TABLE members ADD COLUMN {ine}church TEXT")]
# Postgres only: lock the app's tables so the public (publishable) key used by the welcome form can't read them.
PG_SECURITY = [f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"
               for t in ("members", "services", "attendance", "settings", "email_log", "activity_log")]
# Sign-ups from the public welcome form. On Postgres this table is created by the setup SQL (Members → Setup),
# because it needs Row Level Security and a UUID default; here it only exists for the SQLite demo.
DEMO_REGISTRATIONS = """CREATE TABLE IF NOT EXISTS registrations (
    id TEXT PRIMARY KEY, full_name TEXT, phone TEXT, email TEXT, invited_by TEXT, first_visit DATE, notes TEXT,
    wants_contact BOOLEAN DEFAULT TRUE, status TEXT DEFAULT 'pending', created_at TEXT, member_id TEXT)"""
REG_COLS = ["full_name", "phone", "email", "invited_by", "first_visit", "notes", "wants_contact", "status",
            "created_at", "member_id"]


def _txt(v) -> str:
    """Normalise DB values (Postgres returns date objects, SQLite returns strings) to plain strings."""
    if v is None:
        return ""
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


class DbUnavailable(Exception):
    """The database can't be reached right now. Carries a plain-English reason for the page to show."""


class AlreadyHandled(Exception):
    """Someone else got there first (e.g. another admin already approved this sign-up)."""


def _friendly(err: Exception) -> str:
    msg = str(err).lower()
    if "password authentication failed" in msg:
        return ("The database refused the password. Check `database_url` in the app's Secrets — "
                "it must contain the current Supabase database password.")
    if "circuit" in msg or "too many" in msg or "max client" in msg:
        return ("The database has paused logins for a moment after repeated failed attempts. "
                "Wait a few minutes, check the password in Secrets, then try again.")
    if "timeout" in msg or "timed out" in msg or "could not translate host" in msg or "resolve" in msg:
        return "Couldn't reach the database server. It may be paused or the internet connection dropped."
    return "The database isn't responding right now."


class SqlStore:
    """One store for both engines. Queries use standard SQL that runs unchanged on SQLite and Postgres.

    If Postgres can't be reached, the store backs off (30 s, doubling up to 5 min) and raises DbUnavailable
    straight away during that time — so auto-refreshing pages don't hammer the server and get locked out.
    """

    def __init__(self, url: str | None):
        self.demo = not url
        self.url = url
        self.engine = "sqlite" if self.demo else "postgres"
        self.path = "/tmp/fcc_attendance_demo.db"
        self._lock = threading.RLock()
        self._conn = None
        self._cache = {}
        self._in_tx = False
        self.versioned = False  # set by _ensure_schema once the members.version column is known to exist
        self.has_pastor = False  # likewise for members.pastor
        self.has_age = False  # and members.age_group (Adult / Child)
        self.has_church = False  # and members.church (which branch a person belongs to; blank = home church)
        self._wrote = {}  # service date -> time of the last tick/untick, so a poll never shows an older read
        self._schema_ready = False
        self._down_until = 0.0
        self._backoff = 0.0
        self.last_error = ""
        if self.demo:
            import os
            if os.path.exists(self.path):
                os.remove(self.path)  # fresh demo on every app start
            self._ensure_schema()
            _seed_demo(self)

    def _ensure_schema(self):
        if not self._schema_ready:
            for stmt in SCHEMA + ([DEMO_REGISTRATIONS] if self.demo else []):
                self._raw(stmt)
            for flag, stmt in MIGRATIONS:  # best effort: a missing column only switches its own feature off
                setattr(self, flag, True)
                try:
                    self._raw(stmt.format(ine="" if self.demo else "IF NOT EXISTS "))
                except Exception as e:
                    if "duplicate column" in str(e).lower():
                        continue
                    if type(e).__name__ == "OperationalError":
                        raise
                    setattr(self, flag, False)
            if not self.demo:
                import psycopg
                for stmt in PG_SECURITY:  # best effort: never block the app if the role can't alter a table
                    try:
                        self._raw(stmt)
                    except psycopg.OperationalError:
                        raise
                    except Exception:
                        pass
            self._schema_ready = True

    def retry_in(self) -> int:
        """Seconds until the next connection attempt is allowed (0 = now)."""
        return max(0, int(self._down_until - dt.datetime.now().timestamp()) + 1) if self._down_until else 0

    def retry_now(self):
        self._down_until = 0.0

    def _mark_down(self, err: Exception):
        self._backoff = min(max(self._backoff * 2, 30.0), 300.0)
        self._down_until = dt.datetime.now().timestamp() + self._backoff
        self.last_error = _friendly(err)
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _check_up(self):
        if self._down_until and dt.datetime.now().timestamp() < self._down_until:
            raise DbUnavailable(self.last_error)

    # -- connection handling
    def _connect(self):
        if self.demo:
            import sqlite3
            c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")
            return c
        import psycopg
        return psycopg.connect(self.url, autocommit=True, connect_timeout=10)

    def _q(self, sql: str) -> str:
        return sql if self.demo else sql.replace("?", "%s")

    def _raw(self, sql, params=(), many=False, fetch=False):
        if self._conn is None:
            self._conn = self._connect()
        cur = self._conn.cursor()
        if many:
            cur.executemany(self._q(sql), params)
        else:
            cur.execute(self._q(sql), params)
        if fetch:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        return cur.rowcount  # rows changed: 0 means a conditional write found nothing to change

    def _exec(self, sql, params=(), many=False, fetch=False):
        if self.demo:
            with self._lock:
                return self._raw(sql, params, many, fetch)
        import psycopg
        with self._lock:
            self._check_up()
            # one quiet reconnect if the server dropped an idle connection (never inside a transaction:
            # a new connection would silently run the rest outside it)
            for attempt in ((2,) if self._in_tx else (1, 2)):
                try:
                    self._ensure_schema()
                    out = self._raw(sql, params, many, fetch)
                    self._backoff, self._down_until = 0.0, 0.0
                    return out
                except psycopg.OperationalError as e:  # connection-level problem (not a bad query)
                    stale = self._conn is not None and attempt == 1  # an old connection went away: reconnect once
                    if not stale:
                        self._mark_down(e)
                        raise DbUnavailable(self.last_error) from e
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                except psycopg.InterfaceError as e:  # connection already closed
                    self._conn = None
                    if attempt == 2:
                        self._mark_down(e)
                        raise DbUnavailable(self.last_error) from e

    @contextmanager
    def transaction(self):
        """All-or-nothing block: every write inside commits together, or none do. Nested calls join the outer one."""
        with self._lock:
            if self._in_tx:
                yield
                return
            self._exec("SELECT 1")  # connect (and reconnect if needed) before starting
            self._conn.execute("BEGIN")
            self._in_tx = True
            try:
                yield
            except BaseException:
                self._in_tx = False
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    self._conn = None  # connection is gone, and the server drops the half-done work with it
                raise
            self._in_tx = False
            try:
                self._conn.execute("COMMIT")
            except Exception as e:
                self._conn = None
                self._mark_down(e)
                raise DbUnavailable(self.last_error) from e

    def log(self, kind: str, detail: str = "", member_id: str | None = None, service_date: str | None = None,
            by: str = "", result: str = "done"):
        """Add a line to the append-only activity log (never updated or deleted by the app)."""
        self._exec("INSERT INTO activity_log (id, at, kind, service_date, member_id, detail, by_name, result) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (new_id(), now_iso(), kind, service_date or None, member_id, detail[:500], by[:80], result))

    def activity(self, day: str | None = None, limit: int = 300) -> list[dict]:
        where, params = ("WHERE service_date = ? OR substr(at, 1, 10) = ?", (day, day)) if day else ("", ())
        rows = self._exec(f"SELECT at, kind, service_date, member_id, detail, by_name, result FROM activity_log "
                          f"{where} ORDER BY at DESC LIMIT {int(limit)}", params, fetch=True)
        return [{k: _txt(v) for k, v in r.items()} for r in rows]

    def _cached(self, key, ttl, fn):
        now = dt.datetime.now().timestamp()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    # -- reads
    def list_members(self):
        def load():
            rows = self._exec("SELECT * FROM members", fetch=True)
            return [{k: _txt(v) for k, v in r.items()} | {"group": _txt(r.get("group_name"))} for r in rows]
        return self._cached("members", 60, load)

    def list_services(self):
        def load():
            svcs = {_txt(r["service_date"]): dict(date=_txt(r["service_date"]), name=r["name"] or "", present={})
                    for r in self._exec("SELECT service_date, name FROM services", fetch=True)}
            for r in self._exec("SELECT service_date, member_id, checked_at FROM attendance", fetch=True):
                s = svcs.get(_txt(r["service_date"]))
                if s is not None:
                    s["present"][r["member_id"]] = _txt(r["checked_at"])
            return list(svcs.values())
        return self._cached("services", 15, load)

    def get_service(self, date: str, max_age: float = 2.0):
        """Live-poll read. Shared by every open phone for `max_age` seconds, so ten ushers polling cost one query,
        but a result is never reused if someone ticked after it was read."""
        import time
        key, now = f"svc:{date}", time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < max_age and hit[0] >= self._wrote.get(date, 0.0):
            return hit[1]
        val = self._load_service(date)
        self._cache[key] = (now, val)
        return val

    def _load_service(self, date: str):
        rows = self._exec("SELECT a.member_id, a.checked_at, s.name FROM services s "
                          "LEFT JOIN attendance a ON a.service_date = s.service_date WHERE s.service_date = ?",
                          (date,), fetch=True)
        if not rows:
            return None
        return dict(date=date, name=rows[0]["name"] or "",
                    present={r["member_id"]: _txt(r["checked_at"]) for r in rows if r["member_id"]})

    # -- writes
    def ensure_service(self, date: str, name: str):
        self._exec("INSERT INTO services (service_date, name) VALUES (?, ?) "
                   "ON CONFLICT (service_date) DO UPDATE SET name = excluded.name", (date, name or "Service"))
        self._touched(date)

    def _member_cols(self) -> list[str]:
        return [c for c in MEMBER_COLS if c not in OPTIONAL_COLS or getattr(self, OPTIONAL_COLS[c])]

    def clear_service(self, date: str, by: str = "", only: set | None = None) -> int:
        """Untick everyone for one service in a single step (`only`: just these people, i.e. one church).
        Returns how many ticks were removed."""
        with self.transaction():
            if only is None:
                n = self._exec("DELETE FROM attendance WHERE service_date = ?", (date,))
            else:
                n = sum(self._exec("DELETE FROM attendance WHERE service_date = ? AND member_id = ?", (date, mid))
                        for mid in only)
            self.log("clear_service", f"{n} {'person' if n == 1 else 'people'} unticked at once", None, date, by)
        self._touched(date)
        return n

    def set_present(self, date: str, mid: str, present: bool, name: str = "Sunday Service",
                    seen: str | None = None, by: str = "") -> str:
        """Make this person present or absent. Returns 'done', 'already' (it was already that way) or 'changed'.

        A tick is a target state, so doing it twice changes nothing. An untick with `seen` (the check-in time the
        usher was looking at) only removes that exact tick: if another phone unticked and re-ticked the person
        meanwhile, it returns 'changed' and leaves the newer tick alone.
        """
        with self.transaction():
            if present:
                self._exec("INSERT INTO services (service_date, name) VALUES (?, ?) "
                           "ON CONFLICT (service_date) DO NOTHING", (date, name))
                n = self._exec("INSERT INTO attendance (service_date, member_id, checked_at) VALUES (?, ?, ?) "
                               "ON CONFLICT (service_date, member_id) DO NOTHING", (date, mid, now_iso()))
                result = "done" if n else "already"
            else:
                sql, args = "DELETE FROM attendance WHERE service_date = ? AND member_id = ?", [date, mid]
                if seen:
                    sql, args = sql + " AND checked_at = ?", args + [seen]
                n = self._exec(sql, tuple(args))
                if n:
                    result = "done"
                else:
                    still = self._exec("SELECT 1 AS x FROM attendance WHERE service_date = ? AND member_id = ?",
                                       (date, mid), fetch=True)
                    result = "changed" if still else "already"
            self.log("tick" if present else "untick", "", mid, date, by, result)
        self._touched(date)
        return result

    def _touched(self, date: str):
        import time
        self._wrote[date] = time.time()
        self._cache.pop("services", None)
        self._cache.pop(f"svc:{date}", None)

    def upsert_members(self, rows: list[dict]):
        existing = {r["id"] for r in self._exec("SELECT id FROM members", fetch=True)}
        inserts, updates, mcols = [], [], self._member_cols()
        for r in rows:
            r = dict(r)
            if "group" in r:
                r["group_name"] = r.pop("group")
            mid = r.pop("id", None) or new_id()
            vals = [None if (r.get(c) == "" and c in ("date_joined", "first_visit")) else r.get(c) for c in mcols]
            (updates if mid in existing else inserts).append((mid, vals))
        if inserts:
            cols = ", ".join(["id"] + mcols)
            marks = ", ".join(["?"] * (len(mcols) + 1))
            self._exec(f"INSERT INTO members ({cols}) VALUES ({marks}) ON CONFLICT (id) DO NOTHING",
                       [[mid] + vals for mid, vals in inserts], many=True)
        if updates:  # only overwrite fields that were provided; COALESCE keeps what's already stored
            sets = ", ".join(f"{c} = COALESCE(?, {c})" for c in mcols) + (", version = version + 1" if self.versioned else "")
            self._exec(f"UPDATE members SET {sets} WHERE id = ?", [vals + [mid] for mid, vals in updates], many=True)
        self._cache.pop("members", None)
        return len(rows)

    def update_members(self, changes: dict[str, dict], versions: dict[str, int] | None = None,
                       by: str = "") -> tuple[list[str], list[str]]:
        """Save edits from the register. Returns (saved ids, conflicting ids).

        Optimistic locking: with `versions` (each row's version when the editor opened), a row is only saved if
        nobody else has changed it since. Otherwise it is left alone and reported as a conflict.
        """
        saved, conflicts = [], []
        for mid, fields in changes.items():
            fields = dict(fields)
            if "group" in fields:
                fields["group_name"] = fields.pop("group")
            cols = [c for c in fields if c in self._member_cols() and c != "created_at"]
            if not cols:
                continue
            vals = [None if (fields[c] in ("", None) and c in ("date_joined", "first_visit")) else fields[c] for c in cols]
            bump = ", version = version + 1" if self.versioned else ""
            sql = f"UPDATE members SET {', '.join(f'{c} = ?' for c in cols)}{bump} WHERE id = ?"
            args = vals + [mid]
            if self.versioned and versions and versions.get(mid) not in (None, ""):
                sql, args = sql + " AND version = ?", args + [int(versions[mid])]
            with self.transaction():
                if self._exec(sql, tuple(args)):
                    saved.append(mid)
                    self.log("edit", ", ".join(c.replace("group_name", "group") for c in cols), mid, None, by)
                else:
                    conflicts.append(mid)
                    self.log("edit", ", ".join(cols), mid, None, by, "changed")
        self._cache.pop("members", None)
        return saved, conflicts

    # -- sign-ups from the welcome form
    def list_registrations(self, status: str = "pending") -> list[dict] | None:
        """Sign-ups with this status, oldest first. None if the registrations table isn't set up yet."""
        cols = ", ".join(["id"] + REG_COLS)
        try:
            rows = self._exec(f"SELECT {cols} FROM registrations WHERE status = ? ORDER BY created_at", (status,),
                              fetch=True)
        except DbUnavailable:
            raise
        except Exception as e:  # table or a column missing → the setup SQL hasn't been run
            self.last_setup_error = str(e).strip().splitlines()[0][:300]
            return None
        out = []
        for r in rows:
            d = {k: _txt(v) for k, v in r.items()}
            d["wants_contact"] = bool(r.get("wants_contact")) if r.get("wants_contact") is not None else True
            out.append(d)
        return out

    def count_pending(self) -> int:
        regs = self._cached("pending", 30, lambda: self.list_registrations("pending"))
        return len(regs or [])

    def resolve_registration(self, reg_id: str, status: str, by: str = "") -> bool:
        """Reject (or otherwise close) a pending sign-up. False if someone else already handled it."""
        with self.transaction():
            n = self._exec("UPDATE registrations SET status = ? WHERE CAST(id AS TEXT) = ? AND status = 'pending'",
                           (status, str(reg_id)))
            self.log("signup_" + status, "", None, None, by, "done" if n else "already")
        self._cache.pop("pending", None)
        return bool(n)

    def approve_registration(self, reg: dict, match_id: str | None = None, check_in: bool = True,
                             by: str = "") -> str:
        """Add the sign-up to the register (or fill gaps on the matched person), optionally tick them present.

        One all-or-nothing transaction. It starts by claiming the sign-up ('pending' → 'approved'); if two admins
        press Approve together, the database lets only one claim succeed and the other gets AlreadyHandled,
        so nobody is added twice. If anything fails part-way, the sign-up goes back to pending untouched.
        """
        visit = reg.get("first_visit") or today().isoformat()
        fields = dict(full_name=" ".join(reg["full_name"].split()), phone=reg.get("phone", "").strip(),
                      email=reg.get("email", "").strip(), invited_by=reg.get("invited_by", "").strip())
        try:
            with self.transaction():
                if not self._exec("UPDATE registrations SET status = 'approved' "
                                  "WHERE CAST(id AS TEXT) = ? AND status = 'pending'", (str(reg["id"]),)):
                    raise AlreadyHandled(reg.get("full_name", ""))
                if match_id:  # existing person: only fill in what the form provided, never blank anything
                    self.upsert_members([dict(id=match_id, **{k: v for k, v in fields.items() if v and k != "full_name"})])
                    mid = match_id
                else:
                    mid = new_id()
                    note = reg.get("notes", "").strip()
                    follow = "Welcome form" + ("" if reg.get("wants_contact", True) else " · prefers no contact")
                    self.upsert_members([dict(id=mid, **fields, type="first_timer", status="", first_visit=visit,
                                              follow_up=follow + (f" · {note}" if note else ""), created_at=now_iso())])
                if check_in:
                    self.set_present(visit, mid, True, by=by)
                self._exec("UPDATE registrations SET member_id = ? WHERE CAST(id AS TEXT) = ?", (mid, str(reg["id"])))
                self.log("signup_approved", "matched existing person" if match_id else "new first-timer", mid, None, by)
        finally:
            self._cache.pop("pending", None)
            self._cache.pop("members", None)
        return mid

    # -- settings & email log (daily report)
    def get_setting(self, key: str, default: str = "") -> str:
        rows = self._exec("SELECT value FROM settings WHERE key = ?", (key,), fetch=True)
        return rows[0]["value"] if rows and rows[0]["value"] is not None else default

    def set_setting(self, key: str, value: str):
        self._exec("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                   (key, value))

    def log_email(self, kind: str, report_date: str, recipients: list[str], ok: bool, detail: str = ""):
        self._exec("INSERT INTO email_log (id, kind, report_date, sent_at, recipients, ok, detail) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (new_id(), kind, report_date, now_iso(), ", ".join(recipients), ok,
                                                    detail[:500]))

    def sent_on(self, report_date: str, kind: str = "daily") -> bool:
        rows = self._exec("SELECT COUNT(*) AS n FROM email_log WHERE report_date = ? AND kind = ? AND ok = ?",
                          (report_date, kind, True), fetch=True)
        return bool(rows and rows[0]["n"])

    def email_history(self, limit: int = 15) -> list[dict]:
        rows = self._exec("SELECT kind, report_date, sent_at, recipients, ok, detail FROM email_log "
                          "ORDER BY sent_at DESC LIMIT ?", (limit,), fetch=True)
        return [{k: _txt(v) if k != "ok" else bool(v) for k, v in r.items()} for r in rows]

    # -- read-only SQL for the query page
    def run_query(self, sql: str, limit: int = 5000) -> pd.DataFrame:
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            raise ValueError("Type a query first.")
        if ";" in sql:
            raise ValueError("Run one statement at a time.")
        if self.demo:
            import sqlite3
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)  # read-only at the file level
            try:
                cur = conn.execute(sql)
                cols = [d[0] for d in cur.description or []]
                return pd.DataFrame(cur.fetchmany(limit), columns=cols)
            finally:
                conn.close()
        import psycopg
        self._check_up()
        try:
            conn = psycopg.connect(self.readonly_url or self.url, connect_timeout=10)
        except psycopg.OperationalError as e:
            self._mark_down(e)
            raise DbUnavailable(self.last_error) from e
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")         # the database itself refuses any write
                cur.execute("SET LOCAL statement_timeout = '15s'")
                cur.execute(sql)
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(limit)
            conn.rollback()
        return pd.DataFrame(rows, columns=cols)

    readonly_url = None
    last_setup_error = ""


def _seed_demo(store: "SqlStore"):
    """Invented people + 12 past Sundays of attendance, with a few people who recently stopped coming."""
    rng = np.random.default_rng(7)
    first = ["Ama", "Kwame", "Esi", "Kojo", "Abena", "Yaw", "Akosua", "Kofi", "Adwoa", "Kwabena", "Efua", "Kwaku",
             "Afia", "Kweku", "Aba", "Fiifi", "Naana", "Paa", "Serwaa", "Ekow", "Mia", "Leo", "Zoe", "Eli", "Ruth",
             "Noah", "Tia", "Sam", "Joy", "Ben"]
    last = ["Asante", "Owusu", "Boateng", "Appiah", "Darko", "Ofori", "Quaye", "Tetteh", "Addo", "Badu"]
    names = sorted({f"{rng.choice(first)} {rng.choice(last)}" for _ in range(80)})[:60]
    groups = ["Choir", "Ushering", "Youth", "Media", "Children", "Men", "Women", ""]
    people = [dict(id=new_id(), full_name=n, phone=f"021 {rng.integers(100, 999)} {rng.integers(1000, 9999)}",
                   email="", group=str(rng.choice(groups)), role="", status="", type="member", date_joined="",
                   first_visit="", invited_by="", follow_up="", created_at=now_iso()) for n in names]
    pastors = ["Pastor Ama", "Pastor Kofi", "Pastor Esi", "Pastor Yaw"]
    for i, p in enumerate(people[:40]):  # ten names each; the rest are not assigned yet
        p["pastor"] = pastors[i // 10]
    for p in people:
        p["age_group"] = "Child" if p["group"] == "Children" else "Adult"
    for i, p in enumerate(people[40:]):  # two demo branches, so the all-churches overview has something to show
        p["church"] = "Sydney" if i < 12 else "Melbourne"
    old = (today() - dt.timedelta(days=ARCHIVE_DAYS + 200)).isoformat()
    gone = [dict(id=new_id(), full_name=n, phone="", email="", group="", role="", status="", type="member",
                 date_joined=old, first_visit="", invited_by="", follow_up="", created_at=now_iso(), pastor="",
                 age_group="Adult")
            for n in ("Old Friend One", "Old Friend Two", "Old Friend Three")]
    store.upsert_members([dict(p) for p in people + gone])
    ids = [p["id"] for p in people]
    sundays = [today() - dt.timedelta(days=(today().weekday() + 1) % 7 + 7 * k) for k in range(12)][::-1]
    habit = {m: rng.beta(6, 2) for m in ids}
    stopped = {m: int(rng.integers(3, 9)) for m in rng.choice(ids, 9, replace=False)}
    svc, att = [], []
    for i, d in enumerate(sundays):
        svc.append((d.isoformat(), "Sunday Service"))
        for m, p in habit.items():
            if m in stopped and i >= len(sundays) - stopped[m]:
                continue
            if rng.random() < p:
                att.append((d.isoformat(), m, dt.datetime.combine(d, dt.time(10, int(rng.integers(0, 40))), TZ).isoformat()))
    store._exec("INSERT INTO services (service_date, name) VALUES (?, ?)", svc, many=True)
    store._exec("INSERT INTO attendance (service_date, member_id, checked_at) VALUES (?, ?, ?)", att, many=True)
    regs = [("Grace Mensah", "022 481 2290", "", "Esi Boateng", "Loved the worship — would like to join the choir", True),
            ("Daniel Owusu", "", "daniel.o@example.com", "Instagram", "", False)]
    store._exec("INSERT INTO registrations (id, full_name, phone, email, invited_by, first_visit, notes, wants_contact, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                [(new_id(), n, ph, em, inv, sundays[-1].isoformat(), note, wc, now_iso()) for n, ph, em, inv, note, wc in regs],
                many=True)


@st.cache_resource(show_spinner="Connecting to the database…")
def base_store():
    """The one shared database connection (every church). Pages use get_store(), which is limited to one church."""
    try:
        url = st.secrets.get("database_url")
        ro = st.secrets.get("database_url_readonly")
    except Exception:
        url = ro = None
    store = SqlStore(url or None)
    store.readonly_url = ro or None
    return store


def home_church() -> str:
    """The church everyone belonged to before branches were added (people with no church set)."""
    return _secret("home_church") or "Auckland"


def church_of(m: dict) -> str:
    return (m.get("church") or "").strip() or home_church()


def church_passwords(table: str = "church_passwords") -> dict[str, str]:
    """Branch sign-ins from Secrets:  [church_passwords]  Sydney = "…"  (ushers), and the same shape under
    [church_admin_passwords] for each church's pastor and follow-up leads."""
    try:
        return {str(k): str(v) for k, v in dict(st.secrets.get(table) or {}).items() if v}
    except Exception:
        return {}


def shared_passwords() -> list[str]:
    """Sign-ins that share a password. The password alone decides who someone is, so each must be different;
    with a duplicate, the higher sign-in (admin, Bishop, church admin, ushers, in that order) would win."""
    seen, clash = {}, []
    names = ([("HQ admin", _secret("admin_password")), ("Bishop", _secret("bishop_password"))]
             + [(f"{c} church admin", p) for c, p in church_passwords("church_admin_passwords").items()]
             + [(f"{c} team", p) for c, p in church_passwords().items()]
             + [(f"{home_church()} team", _secret("attendance_password"))])
    for who, pw in names:
        if pw and pw in seen:
            clash.append(f"{seen[pw]} and {who}")
        elif pw:
            seen[pw] = who
    return clash


def all_churches(base) -> list[str]:
    names = {home_church(), *church_passwords(), *church_passwords("church_admin_passwords"),
             *(church_of(m) for m in base.list_members())}
    return [home_church()] + sorted(names - {home_church()}, key=str.lower)


class ChurchStore:
    """A view of the database limited to one church. Everything not listed here passes straight through.

    Pages never see another church's people: members, services, ticks, the activity log and untick-all are all
    filtered to this church, and new people are stamped with it.
    """

    def __init__(self, base, church: str):
        self._base, self.church = base, church

    def __getattr__(self, name):
        return getattr(self._base, name)

    def list_members(self):
        return [m for m in self._base.list_members() if church_of(m) == self.church]

    def _ids(self) -> set:
        return {m["id"] for m in self.list_members()}

    def list_services(self):
        """Only services this church actually ticked people at, so another branch's dates never count as missed."""
        ids, out = self._ids(), []
        for s in self._base.list_services():
            p = {k: v for k, v in (s.get("present") or {}).items() if k in ids}
            if p:
                out.append({**s, "present": p})
        return out

    def get_service(self, date: str, *a, **k):
        s = self._base.get_service(date, *a, **k)
        if not s:
            return s
        ids = self._ids()
        return {**s, "present": {i: t for i, t in (s.get("present") or {}).items() if i in ids}}

    def clear_service(self, date: str, by: str = "") -> int:
        return self._base.clear_service(date, by, only=self._ids())

    def upsert_members(self, rows: list[dict]):
        return self._base.upsert_members([{**r, "church": r.get("church") or self.church} for r in rows])

    def list_registrations(self, status: str = "pending"):
        regs = self._base.list_registrations(status)  # the welcome form belongs to the home church for now
        return regs if regs is None or self.church == home_church() else []

    def count_pending(self) -> int:
        return self._base.count_pending() if self.church == home_church() else 0

    def activity(self, day: str | None = None, limit: int = 300):
        ids, home = self._ids(), self.church == home_church()
        return [r for r in self._base.activity(day, limit) if r["member_id"] in ids or (not r["member_id"] and home)]


def current_church(base) -> str:
    """Which church this visitor is looking at: their own branch, or the one an admin picked in the sidebar."""
    if role(base) == "admin" or base.demo:
        return st.session_state.get("church_pick") or home_church()
    return st.session_state.get("church") or home_church()


def get_store():
    base = base_store()
    return ChurchStore(base, current_church(base))


def show_db_down(err: Exception, key: str = "page"):
    """Friendly 'can't reach the database' panel instead of a traceback, with a manual retry."""
    store = base_store()
    wait = store.retry_in()
    with st.container(border=True):
        st.error(f"**Can't connect to the database.** {err}", icon=":material/cloud_off:")
        st.caption("To avoid getting locked out, the app waits before trying again"
                   + (f" (next automatic try in about {wait} s)." if wait else ".")
                   + " Ticks and edits made while offline are not saved.")
        if st.button("Try again now", icon=":material/refresh:", key=f"db_retry_{key}"):
            store.retry_now()
            st.rerun()


def db_safe(fn):
    """Wrap a page or live fragment so a database outage shows a message, not a crash."""
    import functools

    @functools.wraps(fn)
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except DbUnavailable as e:
            show_db_down(e, fn.__name__)
    return run


# ---------------------------------------------------------------- follow-up logic
def is_child(m: dict) -> bool:
    """Adults and kids are counted separately; a blank age group means adult."""
    return norm(m.get("age_group", "") or "") in {"child", "kid", "kids", "children"}


def split_ages(ids, mem: dict) -> tuple[int, int]:
    """(adults, kids) among these member ids."""
    kids = sum(1 for i in ids if is_child(mem.get(i, {})))
    return len(ids) - kids, kids


def missed_streaks(members: list[dict], services: list[dict], upto: dt.date | None = None,
                   archive: str = "hide") -> pd.DataFrame:
    """Per person: services missed in a row (most recent first), last seen, and a yellow/red flag.

    Only services on or after a person's start (date joined / first visit) count against them.
    `level` is ok / yellow / red (the follow-up rule). `flag` is the same but shows "blue" for someone who is
    still ok yet missed the latest service (1–2 in a row), so leaders can see it early.
    People not seen for ARCHIVE_DAYS (two years) are archived: archive="hide" leaves them out (the default, so
    follow-up lists and counts skip them), "only" returns just them, "all" returns everyone.
    """
    upto = upto or today()
    cutoff = (upto - dt.timedelta(days=ARCHIVE_DAYS)).isoformat()
    svcs = sorted((s for s in services if s.get("date") and s["date"] <= upto.isoformat()), key=lambda s: s["date"])
    rows = []
    for m in members:
        if norm(m.get("status", "")) in INACTIVE:
            continue
        start = min([d for d in (m.get("date_joined"), m.get("first_visit")) if d] or ["0000"])
        mine = [s for s in svcs if s["date"] >= start]
        streak, last_seen = 0, None
        for s in reversed(mine):
            if m["id"] in (s.get("present") or {}):
                last_seen = s["date"]
                break
            streak += 1
        if last_seen is None:  # ticked before their recorded start date still counts as seen
            last_seen = next((s["date"] for s in reversed(svcs) if m["id"] in (s.get("present") or {})), None)
        # last sign of them: last tick, else when they joined / first visited / were added
        ref = last_seen or (start if start != "0000" else (m.get("created_at") or "")[:10])
        archived = bool(ref) and ref < cutoff
        if (archive == "hide" and archived) or (archive == "only" and not archived):
            continue
        attended = sum(m["id"] in (s.get("present") or {}) for s in mine)
        level = "red" if streak >= RED_AT else "yellow" if streak >= YELLOW_AT else "ok"
        rows.append(dict(id=m["id"], name=m.get("full_name", ""), missed=streak, level=level, last_seen=last_seen,
                         attended=attended, eligible=len(mine), phone=m.get("phone", ""), group=m.get("group", ""),
                         type=m.get("type", "member"), invited_by=m.get("invited_by", ""),
                         follow_up=m.get("follow_up", ""), pastor=m.get("pastor", "") or "", archived=archived,
                         since=ref, church=church_of(m), flag=level if level != "ok" else ("blue" if streak else "ok"),
                         age="Child" if is_child(m) else "Adult"))
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["rate"] = np.where(df.eligible > 0, df.attended / df.eligible.clip(lower=1), np.nan)
    order = {"red": 0, "yellow": 1, "ok": 2}
    return df.sort_values(["level", "missed", "name"], key=lambda c: c.map(order) if c.name == "level"
                          else (-c if c.name == "missed" else c.str.lower())).reset_index(drop=True)


LEVEL_LABEL = {"red": f"🔴 Red · {RED_AT}+ missed", "yellow": f"🟡 Yellow · {YELLOW_AT}–{RED_AT - 1} missed",
               "blue": "🔵 Blue · missed this service", "ok": "🟢 On track"}
TINT = {"Red": "rgba(208,59,59,.20)", "Yellow": "rgba(250,178,25,.25)", "Blue": "rgba(90,142,240,.22)"}


def tint_status(col):
    """Row colours for a Status column (used by the follow-up and pastors tables)."""
    return ["background-color: " + next((c for k, c in TINT.items() if k in str(v)), "") for v in col]


# ---------------------------------------------------------------- CSV import (your Google Sheets exports)
def _col(df: pd.DataFrame, *starts: str):
    for c in df.columns:
        if any(c.strip().lower().startswith(s) for s in starts):
            return c
    return None


def _date(v) -> str:
    v = "" if pd.isna(v) else str(v).strip()
    if not v:
        return ""
    d = pd.to_datetime(v, dayfirst=True, errors="coerce")
    return "" if pd.isna(d) else d.date().isoformat()


def _s(v) -> str:
    return "" if pd.isna(v) else " ".join(str(v).split())


def parse_registers(register: pd.DataFrame | None, first_timers: pd.DataFrame | None, existing: list[dict]):
    """Turn the two sheet exports into member records, merging people who appear in both lists
    and matching existing members by name so re-importing updates instead of duplicating."""
    by_name = {norm(m.get("full_name", "")): m["id"] for m in existing}
    out, notes = {}, []
    if register is not None:
        c = dict(name=_col(register, "full name"), phone=_col(register, "phone"), email=_col(register, "email"),
                 joined=_col(register, "date joined"), group=_col(register, "group"),
                 role=_col(register, "ministry", "role"), status=_col(register, "status"),
                 pastor=_col(register, "pastor"))
        reg = register[register[c["name"]].map(_s) != ""]
        dups = reg[c["name"]].map(norm).value_counts()
        for n, k in dups[dups > 1].items():
            notes.append(f"“{n.title()}” appears {k} times in the register — imported once; add the others by hand "
                         "if they are different people.")
        for _, r in reg.iterrows():
            key = norm(r[c["name"]])
            out[key] = dict(full_name=_s(r[c["name"]]), phone=_s(r.get(c["phone"])), email=_s(r.get(c["email"])),
                            date_joined=_date(r.get(c["joined"])), group=_s(r.get(c["group"])),
                            role=_s(r.get(c["role"])), status=_s(r.get(c["status"])), type="member",
                            pastor=_s(r.get(c["pastor"])) if c["pastor"] else "")
    if first_timers is not None:
        c = dict(name=_col(first_timers, "full name"), date=_col(first_timers, "date of visit"),
                 phone=_col(first_timers, "phone"), email=_col(first_timers, "email"),
                 invited=_col(first_timers, "invited"), follow=_col(first_timers, "follow"))
        ft = first_timers[first_timers[c["name"]].map(_s) != ""]
        for _, r in ft.iterrows():
            key = norm(r[c["name"]])
            rec = dict(first_visit=_date(r.get(c["date"])), invited_by=_s(r.get(c["invited"])),
                       follow_up=_s(r.get(c["follow"])))
            if key in out:
                out[key].update({k: v for k, v in rec.items() if v})
                notes.append(f"“{out[key]['full_name']}” is in both lists — kept as a member with their first-visit details.")
            else:
                out[key] = dict(full_name=_s(r[c["name"]]), phone=_s(r.get(c["phone"])), email=_s(r.get(c["email"])),
                                date_joined="", group="", role="", status="", type="first_timer", **rec)
    rows = []
    for key, rec in out.items():
        if key in by_name:
            rec["id"] = by_name[key]
            rec = {k: v for k, v in rec.items() if v or k == "id"}  # never blank out data already in the database
        else:
            rec["created_at"] = now_iso()
        rows.append(rec)
    return rows, notes


# ---------------------------------------------------------------- page helpers
@st.cache_data(show_spinner=False)
def logo_img(css_class: str = "hero-logo") -> str:
    """The church logo as an inline <img> (empty string if the file is missing)."""
    import base64
    from pathlib import Path
    p = Path(__file__).parent / "static" / "logo.png"
    if not p.exists():
        return ""
    return f'<img class="{css_class}" alt="Favourite Child Church" src="data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}">'


def hero_html(eyebrow: str, title: str, subtitle: str, chips: list[str], live: bool = False) -> str:
    dot = '<span class="live-dot"></span>' if live else ""
    return (f'<div class="hero">{logo_img()}<div class="hero-text"><div class="eyebrow">{eyebrow}</div>'
            f'<h1>{dot}{title}</h1><p>{subtitle}</p>' + "".join(f'<span class="chip">{c}</span>' for c in chips)
            + "</div></div>")


def header(title: str, subtitle: str, store, live: bool = False):
    chips = ["Demo data — invented names"] if store.demo else []
    st.html(hero_html("Favourite Child Church · Attendance", title, subtitle, chips, live))


def _secret(name: str) -> str:
    try:
        return st.secrets.get(name) or ""
    except Exception:
        return ""


ROLE_LABEL = {"admin": "Admin", "bishop": "Bishop", "lead": "Church admin", "team": "Team"}
DEMO_ROLES = {"Admin": "admin", "Bishop": "bishop", "Church admin": "lead", "Branch team": "team"}


def role(store) -> str:
    """'admin', 'bishop', 'lead', 'team' or '' (not signed in).

    Passwords in Secrets:
      admin_password            HQ admin: every church, with names, and all the Admin pages
      bishop_password           the all-churches overview only: numbers, never names
      [church_admin_passwords]  one per church (Sydney = "…"): the pastor and follow-up leads of that church;
                                their own church only, including its Members page
      [church_passwords]        one per branch (Sydney = "…"): that church's ushers; no Members page
      attendance_password   the home church's team
    Without admin_password the home team password opens everything, as before. In the demo (no database)
    there are no passwords: the sidebar lets you preview each kind of sign-in.
    """
    if store.demo:
        return DEMO_ROLES.get(st.session_state.get("demo_as"), "admin")
    r = st.session_state.get("role", "")
    if r == "team" and not _secret("admin_password") and st.session_state.get("church", home_church()) == home_church():
        return "admin"
    return r


def is_admin(store) -> bool:
    """HQ admin: every church."""
    return role(store) == "admin"


def can_manage(store) -> bool:
    """HQ admin, or the admin of the church being looked at."""
    return role(store) in ("admin", "lead")


def gate(store, admin: bool = False, hq: bool = False) -> bool:
    """Sign-in check at the top of every page. admin=True needs a church admin or HQ admin; hq=True needs HQ."""
    r = role(store)
    allowed = ("admin",) if hq else ("admin", "lead") if admin else ("admin", "lead", "team")
    if r == "bishop":
        st.warning("The Bishop's sign-in shows the all-churches overview only.", icon=":material/lock:")
        return False
    if store.demo:
        if r not in allowed:
            st.warning("This page is for admins.", icon=":material/lock:")
            return False
        return True
    team_pw, admin_pw = _secret("attendance_password"), _secret("admin_password")
    if not team_pw:
        st.error("Set `attendance_password` in the app's Secrets before real member data can be shown.")
        return False
    if r in allowed:
        return True
    if r:
        st.warning("This page is for admins. Sign out and sign in with the admin password to use it.",
                   icon=":material/lock:")
        return False
    sign_in_form()
    return False


def sign_in_form():
    team_pw, admin_pw, bishop_pw = _secret("attendance_password"), _secret("admin_password"), _secret("bishop_password")
    with st.form("att_login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            found = None
            if admin_pw and entered == admin_pw:
                found = ("admin", home_church())
            elif bishop_pw and entered == bishop_pw:
                found = ("bishop", "")
            elif entered and entered in church_passwords("church_admin_passwords").values():
                found = ("lead", next(c for c, p in church_passwords("church_admin_passwords").items() if p == entered))
            elif entered and entered in church_passwords().values():
                found = ("team", next(c for c, p in church_passwords().items() if p == entered))
            elif team_pw and entered == team_pw:
                found = ("team", home_church())
            if found:
                st.session_state.role, st.session_state.church = found
                st.rerun()
            st.error("Wrong password.")


def actor(store) -> str:
    """Name for the activity log: the name typed on the check-in tab, plus the sign-in type."""
    who = ROLE_LABEL.get(role(store), "Team")
    name = " ".join(str(st.session_state.get("by_name", "")).split())[:40]
    return f"{name} ({who})" if name else who


def account_box(store):
    """Sidebar: an account badge for whoever is signed in, the church they are looking at, and sign out."""
    base = getattr(store, "_base", store)
    r = role(base)
    if not r:
        return
    with st.sidebar:
        if base.demo:
            st.selectbox("Preview as", list(DEMO_ROLES), key="demo_as",
                         help="The demo has no passwords, so you can try each kind of sign-in here.")
            r = role(base)
        name = " ".join(str(st.session_state.get("by_name", "")).split())
        initials = "".join(w[0] for w in name.split()[:2]).upper() or ROLE_LABEL[r][0]
        where = "All churches · numbers only" if r == "bishop" else current_church(base)
        st.html(f'<div class="acct"><span class="acct-pic">{_esc(initials)}</span><span class="acct-text">'
                f'<b>{_esc(name or ROLE_LABEL[r])}</b><small>{ROLE_LABEL[r]} · {_esc(where)}</small></span></div>')
        if r == "admin" or (base.demo and r != "bishop"):
            try:
                names = all_churches(base)
            except DbUnavailable:
                names = [home_church()]
            st.radio("Church", names, key="church_pick",
                     help="Admins can open any church. Branch teams only ever see their own.")
        if r == "admin" and not base.demo and shared_passwords():
            st.warning("These sign-ins share a password, so the app can't tell them apart: "
                       + "; ".join(shared_passwords()) + ". Give each one a different password in Secrets.",
                       icon=":material/key:")
        if not base.demo and st.button("Sign out", icon=":material/logout:", key="sign_out"):
            for k in ("role", "church", "church_pick"):
                st.session_state.pop(k, None)
            st.rerun()


def layout_prefs(names: bool = False):
    """Sidebar layout controls. Streamlit can't drag-resize panels, so layout is chosen here instead."""
    with st.sidebar:
        st.markdown("**Layout**")
        mode = st.segmented_control("Panels", ["Side by side", "Stacked"], default="Side by side",
                                    key="lay_mode") or "Side by side"
        per_row = st.select_slider("Names per row", options=[1, 2, 3, 4], value=3, key="lay_names") if names else 3
    return mode, per_row


def demo_note(store):
    if store.demo:
        st.info("These pages are showing **invented demo people**. Connect your Postgres database (see *Members → Setup*) "
                "to use your real register.", icon=":material/science:")


# ---------------------------------------------------------------- pages
def checkin_panel(store):
    """Ushers tick people as they arrive; ticks from every phone sync within seconds."""
    _, per_row = layout_prefs(names=True)
    c1, c2, c3, c4 = st.columns([1.1, 1.5, 2, 1.5], vertical_alignment="bottom")
    day = c1.date_input("Service date", value=today(), format="DD/MM/YYYY")
    svc_name = c2.text_input("Service", value="Sunday Service")
    q = c3.text_input("Find a person", placeholder="Type a name…", key="ci_q")
    st.session_state.setdefault("ci_by", st.session_state.get("by_name", ""))
    c4.text_input("Ticked by (your name)", placeholder="Optional", key="ci_by", max_chars=40,
                  on_change=lambda: st.session_state.update(by_name=st.session_state.ci_by),
                  help="Only used in the admins' Activity log, so they can see who ticked or unticked someone. "
                       "Leave it blank if you like.")
    date = day.isoformat()
    seen_key = f"ci_seen_{date}"  # the check-in times this phone is showing, for safe unticks

    members = sorted(store.list_members(), key=lambda m: norm(m.get("full_name", "")))
    members = [m for m in members if norm(m.get("status", "")) not in INACTIVE]
    by_id = {m["id"]: m for m in members}
    old = missed_streaks(members, store.list_services(), archive="only")
    archived = set(old.id) if not old.empty else set()  # not seen for two years: only shown when searched for

    def on_tick(mid):
        present = bool(st.session_state[f"ci_{date}_{mid}"])
        seen = None if present else st.session_state.get(seen_key, {}).get(mid)
        try:
            result = store.set_present(date, mid, present, svc_name, seen=seen, by=actor(store))
        except DbUnavailable:
            st.toast("Not saved — can't reach the database right now.", icon=":material/cloud_off:")
            return
        if result == "changed":
            who = next((m["full_name"] for m in members if m["id"] == mid), "That person")
            st.toast(f"{who} was just ticked in again on another phone, so they're still ticked.",
                     icon=":material/sync_problem:")

    @st.fragment(run_every=3)
    @db_safe
    def live_list():
        s = store.get_service(date) or {}
        present = s.get("present") or {}
        st.session_state[seen_key] = dict(present)
        listed = [m for m in members if m["id"] not in archived or m["id"] in present]
        shown = [m for m in (members if q else listed) if not q or norm(q) in norm(m.get("full_name", ""))]
        adults, kids = split_ages(list(present), by_id)
        k = st.columns(5)
        k[0].metric("Checked in", f"{len(present)}", border=True)
        k[1].metric("Adults", f"{adults}", border=True)
        k[2].metric("Kids", f"{kids}", border=True)
        k[3].metric("Not yet", f"{max(len(listed) - len(present), 0)}", border=True)
        k[4].metric("First-timers", f"{sum(1 for m in members if m['id'] in present and m.get('type') == 'first_timer')}",
                    border=True)
        with card("ci_list"):
            st.caption(f"Live · ticks from other phones appear within a few seconds · showing {len(shown)} of {len(listed)} · A to Z")
            st.html(f"<style>.st-key-ci_grid {{--ci-cols: {per_row};}}</style>")
            with st.container(key="ci_grid"):  # CSS grid (see app.py): A to Z across, one column on a phone
                for m in shown:
                    key = f"ci_{date}_{m['id']}"
                    st.session_state[key] = m["id"] in present  # sync ticks made on other devices
                    tag = " · first-timer" if m.get("type") == "first_timer" else ""
                    tag += " · child" if is_child(m) else ""
                    tag += " · archive" if m["id"] in archived else ""
                    st.checkbox(f"{m['full_name']}{tag}", key=key, on_change=on_tick, args=(m["id"],))

    with st.container(key="live_ci"):  # refreshes quietly (see app.py CSS)
        live_list()

    if can_manage(store):
        with st.expander("Untick everyone for this service", icon=":material/remove_done:"):
            n_now = len((store.get_service(date) or {}).get("present") or {})
            st.caption(f"Removes all {n_now} tick{'s' if n_now != 1 else ''} for **{day:%A %d %B %Y}** in one go. "
                       "People stay on the register, and the Activity log records it. This can't be undone.")
            sure = st.checkbox("Yes, untick everyone for this date", key=f"ci_clear_ok_{date}")
            if st.button("Untick everyone", icon=":material/remove_done:", disabled=not (sure and n_now),
                         key=f"ci_clear_{date}"):
                n = store.clear_service(date, by=actor(store))
                for k_ in [k_ for k_ in st.session_state if str(k_).startswith(f"ci_{date}_")]:
                    del st.session_state[k_]
                st.session_state.pop(f"ci_clear_ok_{date}", None)
                st.toast(f"Unticked {n} {'person' if n == 1 else 'people'} for {day:%d %b}.", icon=":material/remove_done:")
                st.rerun()

    with st.expander("Add someone new and check them in", icon=":material/person_add:"):
        with st.form("ft_add", clear_on_submit=True):
            a, b, c = st.columns(3)
            name = a.text_input("Full name")
            phone = b.text_input("Phone")
            invited = c.text_input("Invited by / how they heard of us")
            kind = a.radio("They are a", ["First-timer", "Member"], horizontal=True,
                           help="First-timer: visiting for the first time. Member: already part of the church "
                                "but not on the list yet.")
            age = b.radio("Adult or child", ["Adult", "Child"], horizontal=True)
            if st.form_submit_button("Add & check in", type="primary") and name.strip():
                existing = {norm(m["full_name"]): m["id"] for m in store.list_members()}
                known = existing.get(norm(name))
                mid = known or new_id()
                first = kind == "First-timer"
                rec = dict(id=mid, full_name=" ".join(name.split()), phone=phone.strip(), invited_by=invited.strip(),
                           age_group=age)
                if not known:  # someone already on the register keeps their type and dates
                    rec.update(type="first_timer" if first else "member", status="", created_at=now_iso(),
                               first_visit=date if first else "", date_joined="" if first else date)
                store.upsert_members([rec])
                store.set_present(date, mid, True, svc_name, by=actor(store))
                store.log("add_person", f"{kind.lower()} ({age.lower()}) added on check-in", mid, date, actor(store))
                st.success(f"{'Welcome, ' if first else ''}{name.strip()}{'!' if first else ''} Checked in"
                           + (" (already on the register)." if known else f" as a {kind.lower()}."))
                st.rerun()


# ---------------------------------------------------------------- dashboard (home)
def fmt_date(v, fmt: str = "%d %b", empty: str = "Not yet") -> str:
    """Date → text; blank, None, NaN or unparseable values become `empty` (people never ticked in have no date)."""
    d = pd.to_datetime(v, errors="coerce") if v is not None and not (isinstance(v, float) and np.isnan(v)) else pd.NaT
    return empty if pd.isna(d) else d.strftime(fmt)


def _esc(v) -> str:
    import html
    return html.escape(str(v or ""))


def _delta(n: float | None, unit: str = "", suffix: str = "vs previous") -> str:
    """Up/down arrow + change. Arrow carries the colour; the words stay in text ink."""
    if n is None:
        return f'<span class="kpi-sub">{_esc(suffix)}</span>'
    arrow, cls = ("▲", "up") if n > 0 else ("▼", "down") if n < 0 else ("■", "flat")
    val = f"{abs(n):.0f}{unit}" if isinstance(n, (int, float)) else _esc(n)
    return f'<span class="kpi-delta {cls}"><i>{arrow}</i> {val}</span> <span class="kpi-sub">{_esc(suffix)}</span>'


def kpi_row(items: list[dict]) -> str:
    cells = "".join(
        f'<div class="kpi"><div class="kpi-top"><span class="kpi-icon">{it.get("icon", "")}</span>'
        f'<span class="kpi-label">{_esc(it["label"])}</span></div>'
        f'<div class="kpi-value">{it["value"]}</div><div class="kpi-foot">{it.get("foot", "")}</div></div>'
        for it in items)
    return f'<div class="kpi-grid">{cells}</div>'


def _ago(iso: str) -> str:
    t = pd.to_datetime(iso, errors="coerce", utc=True)
    if pd.isna(t):
        return ""
    mins = (pd.Timestamp.now(tz="UTC") - t).total_seconds() / 60
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins:.0f} min ago"
    if mins < 60 * 24:
        return f"{mins / 60:.0f} h ago"
    return t.tz_convert(TZ).strftime("%a %d %b")


def feed(title: str, rows: list[tuple[str, str, str]], empty: str) -> str:
    """rows: (icon, main text (already escaped/HTML), small muted text)."""
    body = "".join(f'<li><span class="feed-ic">{ic}</span><div><div>{main}</div><small>{_esc(sub)}</small></div></li>'
                   for ic, main, sub in rows) or f'<li class="feed-empty">{_esc(empty)}</li>'
    return f'<div class="feed"><h4>{_esc(title)}</h4><ul>{body}</ul></div>'


@db_safe
def page_dashboard():
    store = get_store()
    header("Dashboard", "Attendance at a glance — updates itself every 30 seconds", store, live=True)
    if not gate(store):
        return
    demo_note(store)

    @st.fragment(run_every=30)
    @db_safe
    def body():
        members, services = store.list_members(), store.list_services()
        mem = {m["id"]: m for m in members}
        tday = today().isoformat()
        past = sorted([x for x in services if x.get("date", "") <= tday], key=lambda x: x["date"])
        df = missed_streaks(members, services)
        pending = store.count_pending()
        if not past:
            st.info("No services recorded yet — tick people on **Follow-up & Check-in → Check-in** to get started.")
            return

        counts = [len(x.get("present") or {}) for x in past]
        last, prev = past[-1], (past[-2] if len(past) > 1 else None)
        n_last, n_prev = counts[-1], (counts[-2] if len(counts) > 1 else None)
        adults, kids = split_ages(list(last.get("present") or {}), mem)
        red = int((df.level == "red").sum()) if not df.empty else 0
        yellow = int((df.level == "yellow").sum()) if not df.empty else 0
        blue = int((df.flag == "blue").sum()) if not df.empty else 0
        ok = (int((df.level == "ok").sum()) if not df.empty else 0) - blue
        month = tday[:7]
        ft_month = [m for m in members if (m.get("first_visit") or "").startswith(month)]
        last_day = dt.date.fromisoformat(last["date"])

        st.html(kpi_row([
            dict(icon="👥", label=f"This service · {last_day:%d %b}", value=n_last,
                 foot=f'<span class="kpi-sub">{_esc(last.get("name") or "Service")} · people present</span>'),
            dict(icon="🧒", label=f"Adults and kids · {last_day:%d %b}", value=f"{adults} + {kids}",
                 foot=f'<span class="kpi-sub">{adults} adult{"s" if adults != 1 else ""} · {kids} '
                      f'kid{"s" if kids != 1 else ""} · {n_last} overall</span>'),
            dict(icon="🔔", label="Need a follow-up call", value=red + yellow,
                 foot=f'<span class="pill red">● {red} red</span> <span class="pill amber">● {yellow} yellow</span> '
                      f'<span class="pill blue">● {blue} missed this service</span>'),
            dict(icon="✨", label=f"First-timers · {today():%B}", value=len(ft_month),
                 foot=(f'<span class="pill blue">{pending} sign-up{"s" if pending != 1 else ""} to approve</span>'
                       if pending else '<span class="kpi-sub">no sign-ups waiting</span>')),
        ]))

        left, right = st.container(key="dash_row").columns([2.2, 1], gap="medium")
        with left:
            a, b = st.container(key="dash_charts").columns([1, 1.35], gap="medium")
            with a:  # donut: where everyone on the register stands
                fig = go.Figure(go.Pie(
                    labels=["On track", "Missed this service", "Yellow", "Red"], values=[ok, blue, yellow, red],
                    hole=0.72, sort=False,
                    marker=dict(colors=[STATUS["ok"], STATUS["blue"], STATUS["yellow"], STATUS["red"]],
                                line=dict(color="#141c22", width=2)),
                    textinfo="none", hovertemplate="%{label}: %{value} people (%{percent})<extra></extra>"))
                fig.add_annotation(text=f"<b style='font-size:30px;color:{INK['primary']}'>{ok + blue + yellow + red}</b>"
                                        f"<br><span style='color:{INK['secondary']}'>on the register</span>",
                                   showarrow=False, x=0.5, y=0.5)
                fig.update_layout(showlegend=True, legend=dict(orientation="h", y=-0.05, x=0.5, xanchor="center"))
                _plot(fig, 330, "Where everyone stands", key="dash_donut")
            with b:  # trend: people present per service, members vs first-timers
                rows = []
                for x in past[-12:]:
                    p = x.get("present") or {}
                    ftn = sum(1 for mid in p if mem.get(mid, {}).get("type") == "first_timer")
                    rows.append(dict(date=pd.to_datetime(x["date"]), members=len(p) - ftn, first_timers=ftn))
                tr = pd.DataFrame(rows)
                fig = go.Figure()
                fig.add_scatter(x=tr.date, y=tr.members, name="Members", mode="lines", stackgroup="one",
                                line=dict(color=SERIES["members"], width=2), fillcolor="rgba(42,166,134,0.35)",
                                hovertemplate="%{y} members<extra></extra>")
                fig.add_scatter(x=tr.date, y=tr.first_timers, name="First-timers", mode="lines", stackgroup="one",
                                line=dict(color=SERIES["first_timers"], width=2), fillcolor="rgba(90,142,240,0.35)",
                                hovertemplate="%{y} first-timers<extra></extra>")
                fig.update_layout(hovermode="x unified")
                fig.update_xaxes(tickformat="%d %b")
                _plot(fig, 330, f"People present · last {len(tr)} services", key="dash_trend")

            with card("dash_followup"):
                st.markdown(f"**Needs a follow-up call** · {red + yellow} people")
                need = df[df.level != "ok"].head(8) if not df.empty else df
                if need.empty:
                    st.caption("Nobody has missed 3 or more services in a row. 🎉")
                else:
                    trs = "".join(
                        f'<tr><td><span class="dot {r.level}"></span>{_esc(r.name)}</td>'
                        f'<td><span class="pill {"red" if r.level == "red" else "amber"}">'
                        f'{"Red" if r.level == "red" else "Yellow"} · {r.missed} missed</span></td>'
                        f'<td>{_esc(fmt_date(r.last_seen))}</td>'
                        f'<td class="muted">{_esc(r.phone) or "—"}</td></tr>' for r in need.itertuples())
                    st.html(f'<table class="dash-table"><thead><tr><th>Name</th><th>Status</th><th>Last seen</th>'
                            f'<th>Phone</th></tr></thead><tbody>{trs}</tbody></table>')
                    if red + yellow > len(need):
                        st.caption(f"+ {red + yellow - len(need)} more on the Follow-up page.")
                    st.download_button("Download the full follow-up list (CSV)", followup_table(df[df.level != "ok"]).to_csv(index=False),
                                       f"follow_up_{tday}.csv", "text/csv", icon=":material/download:", key="dash_fu_dl")

        with right, card("dash_side"):
            today_svc = store.get_service(tday) or {}
            here = today_svc.get("present") or {}
            notes = []
            if pending:
                notes.append(("📝", f"<b>{pending}</b> welcome-form sign-up{'s' if pending != 1 else ''} to approve",
                              "Members → Sign-ups"))
            if red:
                notes.append(("🔴", f"<b>{red}</b> {'person has' if red == 1 else 'people have'} missed {RED_AT}+ in a row",
                              "Follow-up"))
            notes.append(("✅", f"<b>{len(here)}</b> checked in today" if here else "No check-ins yet today",
                          f"{today():%A %d %B}"))
            src = here or (last.get("present") or {})
            arrivals = sorted(src.items(), key=lambda kv: kv[1] or "", reverse=True)[:6]
            act = [("✨" if mem.get(mid, {}).get("type") == "first_timer" else "🙋",
                    _esc(mem.get(mid, {}).get("full_name", "(removed)")), _ago(at)) for mid, at in arrivals]
            call = [] if df.empty else [
                ("📞", f"{_esc(r.name)}", f"{r.phone or 'no phone'} · {r.missed} missed")
                for r in df[df.level == "red"].head(5).itertuples()]
            send_now_button(store, "dash_send_now")
            with st.popover("Summary for WhatsApp", icon=":material/chat:", width="stretch"):
                with_names = st.checkbox("Include names", key="wa_names",
                                         help="Leave off for big group chats; turn on for the leaders' chat.")
                link = st.text_input("Livestream link (optional)", key="wa_link", placeholder="https://…")
                emoji = st.toggle("Emojis", value=True, key="wa_emoji", help="Turn off for a plain-text message.")
                st.code(whatsapp_summary(store, with_names, link, emoji), language=None, wrap_lines=True)
                st.caption("Tap the copy icon at the top right of the box, then paste into WhatsApp.")
            st.html(feed("Notifications", notes, "All caught up")
                    + feed("Latest check-ins" + ("" if here else f" · {last_day:%d %b}"), act, "No check-ins yet")
                    + feed("Call next", call, "No one in red — great!"))
        st.caption(f"Updated {dt.datetime.now(TZ):%H:%M:%S}")

    with st.container(key="live_dash"):  # refreshes quietly (see app.py CSS)
        body()


def church_numbers(base) -> list[dict]:
    """One row of numbers per church. No names: this is all the Bishop's sign-in can load."""
    rows, month = [], today().isoformat()[:7]
    for c in all_churches(base):
        cs = ChurchStore(base, c)
        members, services = cs.list_members(), cs.list_services()
        mem = {m["id"]: m for m in members}
        past = sorted([s for s in services if s["date"] <= today().isoformat()], key=lambda s: s["date"])
        last = past[-1] if past else None
        p = list((last or {}).get("present") or {})
        adults, kids = split_ages(p, mem)
        df = missed_streaks(members, services)
        n = (lambda f: int((df.flag == f).sum()) if not df.empty else 0)
        rows.append(dict(church=c, date=last["date"] if last else "", present=len(p), adults=adults, kids=kids,
                         first=sum(1 for i in p if mem[i].get("type") == "first_timer"),
                         new_month=sum(1 for m in members if (m.get("first_visit") or "").startswith(month)),
                         register=len(df), red=n("red"), yellow=n("yellow"), blue=n("blue"),
                         trend=[len(s["present"]) for s in past[-12:]],
                         change=len(p) - len(past[-2]["present"]) if len(past) > 1 else None))
    return rows


def whatsapp_overview(rows: list[dict], emoji: bool = True) -> str:
    lines = ["*FCC · all churches*" + (" ⛪" if emoji else ""), f"{today():%a %d %b %Y}", "",
             ("✅", f"Present: *{sum(r['present'] for r in rows)}*"),
             ("🧑", f"Adults: *{sum(r['adults'] for r in rows)}*"),
             ("🧒", f"Kids: *{sum(r['kids'] for r in rows)}*"),
             ("👋", f"First-timers: *{sum(r['first'] for r in rows)}*")]
    for r in rows:
        when = f" ({fmt_date(r['date'], '%d %b', '')})" if r["date"] else ""
        flags = f"🔴 {r['red']} · 🟡 {r['yellow']}" if emoji else f"Red {r['red']} · Yellow {r['yellow']}"
        lines += ["", f"*{r['church']}*{when}",
                  f"Present {r['present']} · Adults {r['adults']} · Kids {r['kids']}",
                  f"First-timers {r['first']} · {flags}"]
    return _wa(lines, emoji)


@db_safe
def page_overview():
    base = base_store()
    header("All churches", "Every branch side by side — numbers only, no names", base)
    if role(base) not in ("admin", "bishop"):
        if not role(base) and not base.demo:
            sign_in_form()
        else:
            st.warning("This page is for the Bishop and admins.", icon=":material/lock:")
        return
    demo_note(base)
    rows = church_numbers(base)
    tot = {k: sum(r[k] for r in rows) for k in ("present", "adults", "kids", "first", "register", "red", "yellow", "new_month")}
    st.html(kpi_row([
        dict(icon="⛪", label="Churches", value=len(rows), foot=f'<span class="kpi-sub">{tot["register"]} people on the registers</span>'),
        dict(icon="👥", label="Present · latest services", value=tot["present"],
             foot=f'<span class="kpi-sub">{tot["adults"]} adults · {tot["kids"]} kids</span>'),
        dict(icon="✨", label=f"First-timers · {today():%B}", value=tot["new_month"],
             foot=f'<span class="kpi-sub">{tot["first"]} at the latest services</span>'),
        dict(icon="🔔", label="Need a follow-up call", value=tot["red"] + tot["yellow"],
             foot=f'<span class="pill red">● {tot["red"]} red</span> <span class="pill amber">● {tot["yellow"]} yellow</span>'),
    ]))
    table = pd.DataFrame([{"Church": r["church"], "Latest service": fmt_date(r["date"], "%a %d %b", "None yet"),
                           "Present": r["present"], "Change": r["change"], "Adults": r["adults"], "Kids": r["kids"],
                           "First-timers": r["first"], "On the register": r["register"], "🔴 Red": r["red"],
                           "🟡 Yellow": r["yellow"], "🔵 Missed this service": r["blue"],
                           "Last 12 services": r["trend"]} for r in rows])
    with card("ov_table"):
        st.markdown(f"**Church by church** · {len(rows)} churches")
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "Change": st.column_config.NumberColumn("vs before", format="%+d", help="Compared with that church's previous service"),
            "Last 12 services": st.column_config.LineChartColumn("Last 12 services", y_min=0)})
        st.download_button("Download these numbers (CSV)", table.drop(columns=["Last 12 services"]).to_csv(index=False),
                           f"all_churches_{today().isoformat()}.csv", "text/csv", icon=":material/download:")
    fig = go.Figure()
    names = [r["church"] for r in rows]
    fig.add_bar(x=names, y=[r["adults"] for r in rows], name="Adults", marker_color=SERIES["members"],
                hovertemplate="%{y} adults<extra></extra>")
    fig.add_bar(x=names, y=[r["kids"] for r in rows], name="Kids", marker_color=SERIES["first_timers"],
                hovertemplate="%{y} kids<extra></extra>")
    fig.update_layout(barmode="stack", bargap=0.45, hovermode="x unified")
    with card("ov_chart"):
        _plot(fig, 320, "People present at each church's latest service", key="ov_bar")
    with st.expander("Summary to send by WhatsApp (numbers only)", icon=":material/chat:"):
        emoji = st.toggle("Emojis", value=True, key="ov_emoji", help="Turn off for a plain-text message.")
        st.code(whatsapp_overview(rows, emoji), language=None, wrap_lines=True)
        st.caption("Tap the copy icon at the top right of the box, then paste into WhatsApp.")


@db_safe
def page_followup():
    store = get_store()
    header("Follow-up & Check-in", f"Tick people in on the day, and see who we haven't seen — {RED_AT}+ services "
                                   f"missed in a row is red, {YELLOW_AT}–{RED_AT - 1} is yellow", store, live=True)
    if not gate(store):
        return
    demo_note(store)
    tab_fu, tab_ci, tab_one, tab_pastors, tab_old = st.tabs(
        [":material/notification_important: Needs follow-up", ":material/how_to_reg: Check-in",
         ":material/person_search: One person", ":material/diversity_3: Pastors", ":material/inventory_2: Archive"])
    with tab_fu:
        followup_panel(store)
    with tab_ci:
        checkin_panel(store)
    with tab_one:
        person_panel(store)
    with tab_pastors:
        pastors_panel(store)
    with tab_old:
        archive_panel(store)


def person_panel(store):
    """Pick one person and see every day they came."""
    members = sorted(store.list_members(), key=lambda m: norm(m.get("full_name", "")))
    if not members:
        st.info("No people on the register yet.")
        return
    by_id = {m["id"]: m for m in members}
    mid = st.selectbox("Person", [m["id"] for m in members], index=None, placeholder="Type a name…",
                       format_func=lambda i: by_id[i]["full_name"], key="one_person")
    if not mid:
        st.caption("Choose someone to see the days they came, their attendance rate and when they were last seen.")
        return
    m = by_id[mid]
    services = sorted([s for s in store.list_services() if s.get("date", "") <= today().isoformat()],
                      key=lambda s: s["date"])
    came = [s for s in services if mid in (s.get("present") or {})]
    row = missed_streaks([m], services, archive="all")
    r = row.iloc[0] if not row.empty else None
    facts = [f"{'First-timer' if m.get('type') == 'first_timer' else 'Member'}"]
    for label, key in (("Status", "status"), ("Pastor", "pastor"), ("Group", "group"), ("Phone", "phone")):
        if m.get(key):
            facts.append(f"{label}: {m[key]}")
    st.markdown(f"### {m['full_name']}")
    st.caption(" · ".join(facts))
    k = st.columns(4)
    k[0].metric("Times came", len(came), border=True)
    k[1].metric("Attendance", f"{r.rate:.0%}" if r is not None and pd.notna(r.rate) else "n/a",
                f"{int(r.attended)} of {int(r.eligible)} services" if r is not None else None, delta_color="off", border=True)
    k[2].metric("Last seen", fmt_date(came[-1]["date"], "%d %b %Y") if came else "Not yet", border=True)
    k[3].metric("Missed in a row", int(r.missed) if r is not None else 0,
                "archived" if r is not None and r.archived else None, delta_color="off", border=True)
    recent = services[-26:]
    if recent:
        dots = "".join(f'<span title="{fmt_date(s["date"], "%d %b %Y")}: {"came" if mid in (s.get("present") or {}) else "missed"}" '
                       f'style="display:inline-block;width:14px;height:14px;border-radius:4px;margin:0 3px 3px 0;'
                       f'background:{STATUS["ok"] if mid in (s.get("present") or {}) else "rgba(255,255,255,.10)"}"></span>'
                       for s in recent)
        with card("one_strip"):
            st.markdown(f"**Last {len(recent)} services** · green = came, grey = missed (oldest on the left)")
            st.html(f'<div style="line-height:0">{dots}</div>')
    with card("one_days"):
        st.markdown(f"**Days {m['full_name'].split()[0]} came** · {len(came)}")
        if not came:
            st.caption("No ticks recorded yet.")
        else:
            table = pd.DataFrame([dict(Date=fmt_date(s["date"], "%a %d %b %Y"), Service=s.get("name", ""),
                                       **{"Checked in at": fmt_date(s["present"][mid], "%H:%M", "")})
                                  for s in reversed(came)])
            st.dataframe(table, hide_index=True, width="stretch", height=min(38 * (len(table) + 1) + 4, 420))
            st.download_button("Download these dates (CSV)", table.to_csv(index=False),
                               f"{norm(m['full_name']).replace(' ', '_')}_attendance.csv", "text/csv",
                               icon=":material/download:")


PASTOR_GROUP_SIZE = 10  # default number of people per pastor / shepherd; admins can change it on the Pastors tab


def group_size(store) -> int:
    try:
        return max(1, int(store.get_setting(f"pastor_group_size:{getattr(store, 'church', '')}", "") or PASTOR_GROUP_SIZE))
    except ValueError:
        return PASTOR_GROUP_SIZE


def pastors_panel(store):
    """Each pastor's small group of names, with who was there last service and who needs a call."""
    members, services = store.list_members(), store.list_services()
    df = missed_streaks(members, services)
    if df.empty:
        st.info("No people on the register yet.")
        return
    past = sorted([s for s in services if s.get("date", "") <= today().isoformat()], key=lambda s: s["date"])
    here = set((past[-1].get("present") or {})) if past else set()
    df = df.assign(here=df.id.isin(here), pastor=df.pastor.str.strip())
    named = df[df.pastor != ""]
    if named.empty:
        st.info("No one has a pastor yet. An admin can type a pastor's name in the **Pastor** column under "
                "**Members → Register**, and their lists appear here.",
                icon=":material/diversity_3:")
        return
    summary = (named.groupby("pastor").agg(People=("id", "count"), here=("here", "sum"),
                                           Red=("level", lambda c: int((c == "red").sum())),
                                           Yellow=("level", lambda c: int((c == "yellow").sum()))).reset_index())
    summary = summary.rename(columns={"pastor": "Pastor", "here": "Came this service"})
    size = group_size(store)
    if can_manage(store):
        new = st.number_input("People per pastor or shepherd", min_value=1, max_value=200, value=size, step=1,
                              key="pastor_size", help="How many people each pastor, shepherd or leader looks after. "
                                                      "A ⚠️ shows next to anyone who has more than this.")
        if int(new) != size:
            store.set_setting(f"pastor_group_size:{getattr(store, 'church', '')}", str(int(new)))
            size = int(new)
    summary["People"] = summary.People.map(lambda n: f"{n} of {size}" + (" ⚠️" if n > size else ""))
    with card("pastor_all"):
        st.markdown(f"**All pastors** · {len(summary)} pastors · {len(named)} people assigned · "
                    f"{len(df) - len(named)} not assigned yet")
        st.dataframe(summary, hide_index=True, width="stretch")
    who = st.selectbox("Pastor", sorted(named.pastor.unique(), key=str.lower) + ["Not assigned yet"], key="pastor_pick")
    mine = df[df.pastor == ""] if who == "Not assigned yet" else df[df.pastor == who]
    table = pd.DataFrame({"Status": mine.flag.map(LEVEL_LABEL), "Name": mine.name,
                          "This service": mine.here.map({True: "✅ Came", False: "—"}),
                          "Missed in a row": mine.missed,
                          "Last seen": mine.last_seen.map(lambda v: fmt_date(v, "%d %b %Y")), "Phone": mine.phone})
    with card("pastor_one"):
        st.markdown(f"**{who}** · {len(mine)} {'person' if len(mine) == 1 else 'people'}")
        st.dataframe(table, hide_index=True, width="stretch", height=min(38 * (len(table) + 1) + 4, 460))
    if who != "Not assigned yet":
        with st.expander("Message for this pastor (copy for WhatsApp)", icon=":material/chat:"):
            emoji = st.toggle("Emojis", value=True, key="pastor_emoji", help="Turn off for a plain-text message.")
            st.code(whatsapp_pastor(who, mine, past[-1] if past else None, emoji), language=None, wrap_lines=True)


def archive_panel(store):
    """People not seen for two years. They are left out of follow-up and the check-in list, never deleted."""
    df = missed_streaks(store.list_members(), store.list_services(), archive="only")
    years = ARCHIVE_DAYS // 365
    st.caption(f"People we haven't seen for {years} years move here on their own. They no longer count in follow-up "
               "or the dashboard, and are hidden on Check-in unless you search for their name. "
               "Tick them in when they come back and they return to the normal lists straight away.")
    if df.empty:
        st.success(f"Nobody has been away for {years} years.", icon=":material/done_all:")
        return
    table = pd.DataFrame({"Name": df.name, "Last seen": df.last_seen.map(lambda v: fmt_date(v, "%d %b %Y", "Never ticked")),
                          "On the register since": df.since.map(lambda v: fmt_date(v, "%d %b %Y", "")),
                          "Phone": df.phone, "Pastor": df.pastor, "Group": df.group}).sort_values("Name")
    with card("archive_list"):
        st.markdown(f"**Archive** · {len(table)} {'person' if len(table) == 1 else 'people'}")
        st.dataframe(table, hide_index=True, width="stretch", height=min(38 * (len(table) + 1) + 4, 520))
        st.download_button("Download archive (CSV)", table.to_csv(index=False), "archive.csv", "text/csv",
                           icon=":material/download:")


# ---------------------------------------------------------------- WhatsApp summaries (plain text to copy)
def _names(rows, limit: int = 12) -> str:
    names = [str(n) for n in rows]
    return ", ".join(names[:limit]) + (f" +{len(names) - limit} more" if len(names) > limit else "")


def _wa(lines: list, emoji: bool = True) -> str:
    """Join message lines. Each line is text, or (emoji, text); the emoji is dropped when emojis are off."""
    out = [(f"{ln[0]} {ln[1]}" if emoji else ln[1]) if isinstance(ln, tuple) else ln for ln in lines]
    return "\n".join(out).strip()


def whatsapp_summary(store, names: bool = False, link: str = "", emoji: bool = True) -> str:
    """This week's numbers as a WhatsApp message (*bold* is WhatsApp's own formatting)."""
    members, services = store.list_members(), store.list_services()
    mem = {m["id"]: m for m in members}
    past = sorted([s for s in services if s.get("date", "") <= today().isoformat()], key=lambda s: s["date"])
    if not past:
        return "No services recorded yet."
    last = past[-1]
    p = last.get("present") or {}
    day = dt.date.fromisoformat(last["date"])
    counts = [len(s.get("present") or {}) for s in past]
    change = counts[-1] - counts[-2] if len(counts) > 1 else None
    first = [mem[i]["full_name"] for i in p if mem.get(i, {}).get("type") == "first_timer"]
    df = missed_streaks(members, services)
    red = df[df.level == "red"] if not df.empty else df
    yellow = df[df.level == "yellow"] if not df.empty else df
    moved = "" if not change else f" ({'up' if change > 0 else 'down'} {abs(change)} on last time)"
    adults, kids = split_ages(list(p), mem)
    lines = [f"*FCC {last.get('name') or 'Service'}*" + (" ⛪" if emoji else ""), f"{day:%a %d %b %Y}", "",
             ("✅", f"Present: *{len(p)}*{moved}"),
             ("🧑", f"Adults: *{adults}*"),
             ("🧒", f"Kids: *{kids}*"),
             ("👋", f"First-timers: *{len(first)}*")]
    if names and first:
        lines.append(_names(first))
    lines += ["", "*Follow-up*", ("🔴", f"Missed {RED_AT}+ in a row: *{len(red)}*")]
    if names and len(red):
        lines.append(_names(red.name))
    lines.append(("🟡", f"Missed {YELLOW_AT}–{RED_AT - 1} in a row: *{len(yellow)}*"))
    if names and len(yellow):
        lines.append(_names(yellow.name))
    if link.strip():
        lines += ["", ("📺", f"Livestream: {link.strip()}")]
    return _wa(lines, emoji)


def whatsapp_pastor(pastor: str, mine: pd.DataFrame, last: dict | None, emoji: bool = True) -> str:
    came, missed = mine[mine.here], mine[~mine.here]
    need = mine[mine.level != "ok"]
    lines = [f"*{pastor} · your people*" + (" ⛪" if emoji else "")]
    if last:
        lines.append(f"{dt.date.fromisoformat(last['date']):%a %d %b %Y}")
    lines += ["", ("✅", f"Came: *{len(came)} of {len(mine)}*")]
    if len(came):
        lines.append(_names(came.name, 20))
    if len(missed):
        lines += ["", ("🙏", "Not there:"), _names(missed.name, 20)]
    if len(need):
        rows = [f"- {r.name} ({r.missed} missed{', ' + r.phone if r.phone else ''})" for r in need.itertuples()]
        lines += ["", ("📞", "Please call:"), *rows[:20]]
        if len(rows) > 20:
            lines.append(f"+{len(rows) - 20} more")
    return _wa(lines, emoji)


def _xlsx(table: pd.DataFrame, title: str = "List") -> bytes:
    """A table as an Excel file (opens in Excel, Numbers and Google Sheets)."""
    import io
    buf = io.BytesIO()
    table.to_excel(buf, index=False, sheet_name=title[:31] or "List")
    return buf.getvalue()


def followup_table(view: pd.DataFrame) -> pd.DataFrame:
    """The follow-up list as shown and as downloaded."""
    return pd.DataFrame({
        "Status": view.flag.map(LEVEL_LABEL), "Name": view.name, "Missed in a row": view.missed,
        "Last seen": pd.to_datetime(view.last_seen, errors="coerce").dt.strftime("%d %b %Y").fillna("Not yet"),
        "Attendance": view.rate, "Phone": view.phone, "Church": view.church, "Pastor": view.pastor,
        "Adult / Child": view.age,
        "Group": view.group,
        "Type": view.type.map({"member": "Member", "first_timer": "First-timer"}).fillna(view.type),
        "Invited by": view.invited_by})


def followup_panel(store):
    show = st.segmented_control("Show", ["Needs follow-up", "Red only", "Yellow only", "Missed this service", "Everyone"],
                                default="Needs follow-up", key="fu_show") or "Needs follow-up"
    pastors = sorted({(m.get("pastor") or "").strip() for m in store.list_members()} - {""}, key=str.lower)
    pastor = st.selectbox("Pastor", ["All pastors"] + pastors, key="fu_pastor") if pastors else "All pastors"

    @st.fragment(run_every=30)
    @db_safe
    def live_followup():
        members, services = store.list_members(), store.list_services()
        df = missed_streaks(members, services)
        past = sorted([s for s in services if s.get("date", "") <= today().isoformat()], key=lambda s: s["date"])
        if df.empty or not past:
            st.info("No services recorded yet. Tick people on the **Check-in** tab and this list fills itself in.")
            return
        red, yellow = int((df.level == "red").sum()), int((df.level == "yellow").sum())
        blue = int((df.flag == "blue").sum())
        k = st.columns(5)
        k[0].metric("🔴 Red", red, f"{RED_AT}+ in a row", delta_color="off", border=True)
        k[1].metric("🟡 Yellow", yellow, f"{YELLOW_AT}–{RED_AT - 1} in a row", delta_color="off", border=True)
        k[2].metric("🔵 Blue", blue, "missed this service", delta_color="off", border=True)
        k[3].metric("🟢 On track", int((df.flag == "ok").sum()), border=True)
        last = past[-1]
        k[4].metric("This service", f"{len(last.get('present') or {})} present",
                    dt.date.fromisoformat(last["date"]).strftime("%a %d %b"), delta_color="off", border=True)

        view = {"Needs follow-up": df[df.level != "ok"], "Red only": df[df.level == "red"],
                "Yellow only": df[df.level == "yellow"], "Missed this service": df[df.missed >= 1],
                "Everyone": df}[show]
        if pastor != "All pastors":
            view = view[view.pastor.str.strip() == pastor]
        table = followup_table(view)
        with card("fu_list"):
            st.markdown(f"**{show}** · {len(view)} people · most-missed first")
            st.dataframe(table.style.apply(tint_status, subset=["Status"]), hide_index=True, width="stretch",
                         height=min(38 * (len(table) + 1) + 4, 560),
                         column_config={"Attendance": st.column_config.ProgressColumn(
                             "Attendance", format="percent", min_value=0, max_value=1),
                             "Missed in a row": st.column_config.NumberColumn(format="%d")})
            slug = norm(show).replace(" ", "_")
            d1, d2 = st.columns(2)
            d1.download_button(f"Download this list ({len(table)}) as CSV", table.to_csv(index=False),
                               f"{slug}_{today().isoformat()}.csv", "text/csv", icon=":material/download:",
                               width="stretch")
            d2.download_button(f"Download this list ({len(table)}) for Excel", _xlsx(table, show),
                               f"{slug}_{today().isoformat()}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               icon=":material/table_view:", width="stretch")

        trend = pd.DataFrame([dict(date=s["date"], present=len(s.get("present") or {})) for s in past[-26:]])
        trend["date"] = pd.to_datetime(trend.date)
        fig = px.bar(trend, x="date", y="present", labels={"present": "People present", "date": ""})
        fig.update_traces(marker_color=SERIES["members"], hovertemplate="%{x|%d %b %Y}: %{y} present<extra></extra>")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=40, b=10), bargap=0.25,
                          title=dict(text="Attendance per service", font=dict(size=15)))
        with card("fu_trend"):
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.caption(f"Live · recalculated {dt.datetime.now(TZ):%H:%M:%S} · only services since someone joined "
                   "or first visited count against them.")

    with st.container(key="live_fu"):  # refreshes quietly (see app.py CSS)
        live_followup()


EDIT_COLS = ["full_name", "type", "age_group", "phone", "email", "group", "pastor", "role", "status", "date_joined",
             "invited_by", "follow_up"]
STATUSES = ["Active", "Away", "Inactive", "Moved", "Left", "Transferred", "Deceased"]


def _as_date(v) -> str:
    """Stored ISO date → 'DD/MM/YYYY' for the editor ('' when empty, so the cell shows blank, not None)."""
    d = pd.to_datetime(v, errors="coerce")
    return "" if pd.isna(d) else d.strftime("%d/%m/%Y")


def all_churches_xlsx(base) -> bytes:
    """Everyone on every register as one Excel file: an "Everyone" sheet with a Church column, then a sheet per church."""
    import io
    rows = [{"Church": church_of(m), "Name": m.get("full_name", ""), "Type": "First-timer" if m.get("type") == "first_timer" else "Member",
             "Adult / Child": "Child" if is_child(m) else "Adult", "Phone": m.get("phone", ""), "Email": m.get("email", ""),
             "Pastor": m.get("pastor", ""), "Group": m.get("group", ""), "Status": m.get("status", "") or "Active",
             "Joined": fmt_date(m.get("date_joined"), "%d/%m/%Y", "")} for m in base.list_members()]
    df = pd.DataFrame(rows).sort_values(["Church", "Name"], key=lambda c: c.str.lower())
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xl:
        df.to_excel(xl, index=False, sheet_name="Everyone")
        for c in all_churches(base):
            df[df.Church == c].drop(columns=["Church"]).to_excel(xl, index=False, sheet_name=c[:31])
    return buf.getvalue()


def register_editor(store):
    """Editable register: click a cell, type, then Save. Rows are never deleted here — set Status instead."""
    m = pd.DataFrame(store.list_members())
    if m.empty:
        st.info("No members yet — use **Import CSV** to load your register.")
        return
    hq = is_admin(store)
    cols = EDIT_COLS + (["church"] if hq else [])  # HQ can move someone to another church
    for c in cols:
        if c not in m.columns:
            m[c] = ""
    m["church"] = m.apply(church_of, axis=1)
    versions_now = dict(zip(m["id"], m["version"])) if "version" in m.columns else {}
    m = m.set_index("id")[cols].fillna("")
    for c in ("date_joined",):
        m[c] = m[c].map(_as_date)
    m["status"] = m["status"].map(lambda v: v or "Active")  # blank status means active
    m["age_group"] = m.apply(lambda r: "Child" if is_child(r) else "Adult", axis=1)  # blank means adult
    extra = sorted({s for s in m.status.unique() if s and s not in STATUSES})
    q = st.text_input("Search", placeholder="Filter by name, group, phone…", key="reg_q")
    view = m if not q else m[m.apply(lambda r: q.lower() in " ".join(map(str, r)).lower(), axis=1)]
    view = view.sort_values("full_name", key=lambda s: s.str.lower())
    st.caption(f"{len(view)} of {len(m)} people · click a cell to edit, then **Save changes**. "
               "To take someone off the lists, set Status to Away (travelling, unwell) or Moved/Inactive "
               "(their history is kept).")
    # Optimistic locking: remember each row's version when editing starts; keep it while edits are unsaved.
    ed_key, snap_key = f"reg_{q}", f"reg_versions_{q}"
    if not (st.session_state.get(ed_key) or {}).get("edited_rows") or snap_key not in st.session_state:
        st.session_state[snap_key] = versions_now
    flash = st.session_state.pop("reg_flash", None)
    if flash:
        st.warning(flash, icon=":material/sync_problem:")
    edited = st.data_editor(
        view, key=ed_key, hide_index=True, width="stretch", height=520, num_rows="fixed",
        column_config={
            "full_name": st.column_config.TextColumn("Name", required=True, max_chars=100),
            "type": st.column_config.SelectboxColumn("Type", options=["member", "first_timer"], required=True),
            "age_group": st.column_config.SelectboxColumn("Adult / Child", options=["Adult", "Child"], required=True),
            "phone": st.column_config.TextColumn("Phone", max_chars=30),
            "email": st.column_config.TextColumn("Email", max_chars=120),
            "group": st.column_config.TextColumn("Group"),
            "church": st.column_config.SelectboxColumn("Church", options=all_churches(base_store()), required=True,
                                                       help="Change this to move someone to another church"),
            "pastor": st.column_config.TextColumn("Pastor", help="The pastor who looks after this person", max_chars=60),
            "role": st.column_config.TextColumn("Ministry / role"),
            "status": st.column_config.SelectboxColumn("Status", options=STATUSES + extra),
            "date_joined": st.column_config.TextColumn("Joined", help="DD/MM/YYYY", max_chars=10),
            "invited_by": st.column_config.TextColumn("Invited by"),
            "follow_up": st.column_config.TextColumn("Follow-up notes"),
        })
    changes, bad_dates = {}, []
    for mid in edited.index:
        diff = {}
        for c in cols:
            old, new = view.at[mid, c], edited.at[mid, c]
            old = "" if old is None or (not isinstance(old, str) and pd.isna(old)) else str(old).strip()
            new = "" if new is None or (not isinstance(new, str) and pd.isna(new)) else str(new).strip()
            if c == "status":
                old, new = ("" if v == "Active" else v for v in (old, new))
            if c in ("date_joined", "first_visit"):
                old = _date(old)
                parsed = _date(new)
                if new and not parsed:
                    bad_dates.append(f"{edited.at[mid, 'full_name']}: “{new}”")
                    continue
                new = parsed
            if old != new:
                diff[c] = new
        if diff:
            changes[mid] = diff
    a, b = st.columns([1, 4], vertical_alignment="center")
    save = a.button(f"Save changes ({len(changes)})", type="primary", disabled=not changes, icon=":material/save:")
    if changes:
        b.caption("Unsaved: " + ", ".join(edited.at[mid, "full_name"] or "(no name)" for mid in list(changes)[:6])
                  + ("…" if len(changes) > 6 else ""))
    if save:
        if any(not str(edited.at[mid, "full_name"]).strip() for mid in changes):
            st.error("Every person needs a name.")
        elif bad_dates:
            st.error("Dates need to look like 25/12/2025 — check: " + "; ".join(bad_dates[:5]))
        else:
            saved, clashes = store.update_members(changes, st.session_state.get(snap_key), by=actor(store))
            st.session_state.pop(ed_key, None)
            st.session_state.pop(snap_key, None)
            if saved:
                st.toast(f"Saved {len(saved)} {'person' if len(saved) == 1 else 'people'}.", icon=":material/check_circle:")
            if clashes:
                names = ", ".join(str(edited.at[mid, "full_name"]) for mid in clashes[:5])
                st.session_state.reg_flash = (
                    f"Not saved: {names}. Someone else changed {'this person' if len(clashes) == 1 else 'these people'} "
                    "while you were editing. Their latest details are shown now. Please make your change again.")
            st.rerun()


@db_safe
def page_members():
    store = get_store()
    header("Members", "Your register and first-timers, stored in the database", store)
    if not gate(store, admin=True):
        return
    demo_note(store)
    pending = store.count_pending()
    names = ["Register", f"Sign-ups ({pending})" if pending else "Sign-ups", "Add person", "Import CSV", "Activity"]
    tabs = st.tabs(names + (["Setup"] if store.demo else []))  # Setup is only needed before a database is connected
    tab_list, tab_signups, tab_add, tab_import, tab_activity = tabs[:5]

    with tab_activity:
        activity_panel(store)

    with tab_list:
        register_editor(store)
        if is_admin(store):
            st.download_button("Download every church's register (Excel, one sheet per church)",
                               all_churches_xlsx(base_store()), f"fcc_registers_{today().isoformat()}.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               icon=":material/table_view:")

    with tab_signups:
        signups(store)

    with tab_add:
        with st.form("member_add", clear_on_submit=True):
            a, b = st.columns(2)
            name = a.text_input("Full name")
            kind = b.selectbox("Type", ["member", "first_timer"], format_func=lambda k: k.replace("_", "-").title())
            phone, email = a.text_input("Phone"), b.text_input("Email")
            group, role = a.text_input("Group"), b.text_input("Ministry / role")
            age = a.radio("Adult or child", ["Adult", "Child"], horizontal=True)
            if st.form_submit_button("Add", type="primary") and name.strip():
                store.upsert_members([dict(full_name=" ".join(name.split()), type=kind, phone=phone.strip(),
                                           age_group=age,
                                           email=email.strip(), group=group.strip(), role=role.strip(), status="",
                                           date_joined=today().isoformat() if kind == "member" else "",
                                           first_visit=today().isoformat() if kind == "first_timer" else "",
                                           created_at=now_iso())])
                st.success(f"Added {name.strip()}.")

    with tab_import:
        st.markdown("Upload the CSV exports of your **General Church Register** and **First timers** sheets "
                    "(File → Download → CSV in Google Sheets). Re-importing updates people instead of duplicating them.")
        a, b = st.columns(2)
        reg_file = a.file_uploader("General Church Register (.csv)", type="csv")
        ft_file = b.file_uploader("First timers (.csv)", type="csv")
        if reg_file or ft_file:
            reg = pd.read_csv(reg_file, dtype=str) if reg_file else None
            ft = pd.read_csv(ft_file, dtype=str) if ft_file else None
            rows, notes = parse_registers(reg, ft, store.list_members())
            new = sum("id" not in r for r in rows)
            st.write(f"**{len(rows)}** people found — {new} new, {len(rows) - new} updates.")
            for n in notes:
                st.caption("• " + n)
            st.dataframe(pd.DataFrame(rows).drop(columns=["id", "created_at"], errors="ignore"),
                         hide_index=True, width="stretch", height=300)
            if st.button("Import into the database", type="primary", icon=":material/cloud_upload:"):
                n = store.upsert_members(rows)
                st.success(f"Imported {n} people." + (" (Demo store — this resets when the app restarts.)" if store.demo else ""))

    if store.demo:
        with tabs[5]:
            st.markdown(SETUP_GUIDE)


ACTIVITY_LABELS = {"tick": "Ticked in", "untick": "Unticked", "clear_service": "Unticked everyone", "edit": "Edited", "add_person": "Added",
                   "signup_approved": "Approved sign-up", "signup_rejected": "Rejected sign-up"}
RESULT_LABELS = {"done": "Done", "already": "No change (already done)",
                 "changed": "Blocked: someone else changed it first"}


def activity_panel(store):
    """Append-only history of every tick, untick, edit and approval: who, what, when."""
    st.caption("Every change is added here and never edited or deleted, so you can always see who did what. "
               "“Blocked” means two people acted at the same moment and the app kept the newer change.")
    a, b, _ = st.columns([1, 1, 2], vertical_alignment="bottom")
    day = a.date_input("Day", value=today(), format="DD/MM/YYYY", key="act_day")
    only = b.selectbox("Show", ["Everything", "Check-ins", "Edits & sign-ups", "Blocked only"], key="act_only")
    rows = store.activity(day.isoformat())
    names = {m["id"]: m["full_name"] for m in store.list_members()}
    df = pd.DataFrame([dict(Time=fmt_date(r["at"], "%H:%M:%S", ""), What=ACTIVITY_LABELS.get(r["kind"], r["kind"]),
                            Person=names.get(r["member_id"], "(removed)" if r["member_id"] else ""),
                            Details=r["detail"], By=r["by_name"] or "", Result=RESULT_LABELS.get(r["result"], r["result"]),
                            _kind=r["kind"], _res=r["result"]) for r in rows])
    if df.empty:
        st.info("Nothing recorded on this day yet.", icon=":material/history:")
        return
    if only == "Check-ins":
        df = df[df._kind.isin(["tick", "untick"])]
    elif only == "Edits & sign-ups":
        df = df[~df._kind.isin(["tick", "untick"])]
    elif only == "Blocked only":
        df = df[df._res == "changed"]
    st.dataframe(df.drop(columns=["_kind", "_res"]), hide_index=True, width="stretch", height=460)


def signups(store):
    """Approve or reject sign-ups that came in from the public welcome form."""
    regs = store.list_registrations("pending")
    if regs is None:
        st.warning("The sign-ups table isn't set up yet (or is missing a column). Open Supabase → **SQL editor**, "
                   "paste the SQL below and click **Run**.", icon=":material/construction:")
        if store.last_setup_error:
            st.caption(f"Database said: {store.last_setup_error}")
        st.code(REGISTRATIONS_SQL, language="sql")
        return
    st.caption("People who filled in the welcome form. **Approve** adds them to the register as a first-timer "
               "(or updates the person with the same name) — nothing reaches the register until you do.")
    if not regs:
        st.success("No sign-ups waiting.", icon=":material/done_all:")
        return
    by_name = {norm(m["full_name"]): m for m in store.list_members()}
    for r in regs:
        match = by_name.get(norm(r["full_name"]))
        with card(f"signup_{r['id']}"):
            top = st.columns([3, 2], vertical_alignment="center")
            submitted = _parse_times([r["created_at"]]).iloc[0]
            top[0].markdown(f"**{r['full_name']}**" + (" · :orange[prefers no contact]" if not r["wants_contact"] else ""))
            top[1].caption(f"Sent {submitted:%a %d %b, %H:%M}" if pd.notna(submitted) else "")
            info = [("Phone", r["phone"]), ("Email", r["email"]), ("Invited by / heard via", r["invited_by"]),
                    ("First visit", r["first_visit"])]
            st.markdown("  \n".join(f"{k}: {v}" for k, v in info if v) or "_No contact details given._")
            if r["notes"]:
                st.info(r["notes"], icon=":material/chat:")
            if match:
                st.caption(f":material/link: Already on the register as **{match['full_name']}** "
                           f"({match.get('type', '').replace('_', '-') or 'member'}) — approving updates that person.")
            a, b, c = st.columns([2, 1, 1], vertical_alignment="center")
            tick = a.checkbox(f"Mark present on {r['first_visit'] or 'today'}", value=True, key=f"reg_ci_{r['id']}")
            if b.button("Approve", key=f"reg_ok_{r['id']}", type="primary", icon=":material/check:", width="stretch"):
                try:
                    store.approve_registration(r, match["id"] if match else None, check_in=tick, by=actor(store))
                    st.toast(f"{r['full_name']} added to the register.", icon=":material/person_add:")
                except AlreadyHandled:
                    st.toast(f"{r['full_name']} was already handled by another admin, so nothing was added twice.",
                             icon=":material/info:")
                st.rerun()
            if c.button("Reject", key=f"reg_no_{r['id']}", icon=":material/close:", width="stretch"):
                if store.resolve_registration(r["id"], "rejected", by=actor(store)):
                    st.toast(f"Sign-up from {r['full_name']} rejected.")
                else:
                    st.toast(f"{r['full_name']} was already handled by another admin.", icon=":material/info:")
                st.rerun()


def _parse_times(values) -> pd.Series:
    t = pd.to_datetime(pd.Series(list(values), dtype="object"), errors="coerce", utc=True)
    return t.dt.tz_convert(TZ)


def _style(fig, height=320, title=None):
    """Dark card styling shared by every chart: transparent background, recessive grid, readable ink."""
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=44 if title else 8, b=8),
                      title=dict(text=title, font=dict(size=15, color=INK["primary"])) if title else None,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color=INK["secondary"]),
                      legend=dict(orientation="h", yanchor="top", y=-0.18, x=0, title=None),
                      hoverlabel=dict(bgcolor="#1d2830", bordercolor="#2c3b46", font=dict(color=INK["primary"])))
    fig.update_xaxes(showgrid=False, linecolor=INK["grid"], tickfont=dict(color=INK["secondary"]))
    fig.update_yaxes(gridcolor=INK["grid"], zeroline=False, tickfont=dict(color=INK["secondary"]))
    return fig


def card(name: str):
    """A rounded dark card (bordered container), styled in app.py via its st-key-card_* class."""
    return st.container(border=True, key="card_" + re.sub(r"\W+", "_", name.lower()).strip("_"))


def _plot(fig, height=320, title=None, key=None):
    _style(fig, height, title)
    with card(key or title or "chart"):
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


@db_safe
def page_live():
    store = get_store()
    header("Live", "Who has arrived — updates on its own every few seconds, great on a screen during service",
           store, live=True)
    if not gate(store):
        return
    demo_note(store)
    c1, c2 = st.columns([1, 3], vertical_alignment="bottom")
    day = c1.date_input("Service date", value=today(), format="DD/MM/YYYY", key="live_day")
    every = c2.select_slider("Refresh every", options=[3, 5, 10, 30], value=5, format_func=lambda s: f"{s}s")
    date = day.isoformat()

    @st.fragment(run_every=every)
    @db_safe
    def live_body():
        members = {m["id"]: m for m in store.list_members()}
        s = store.get_service(date) or {}
        present = s.get("present") or {}
        past = sorted([x for x in store.list_services() if x.get("date", "") < date], key=lambda x: x["date"])
        prev = past[-1] if past else None
        prev_n = len(prev.get("present") or {}) if prev else None
        ft_today = [mid for mid in present if members.get(mid, {}).get("type") == "first_timer"]
        new_today = [mid for mid in present if members.get(mid, {}).get("first_visit") == date]
        active = [m for m in members.values() if norm(m.get("status", "")) not in INACTIVE]

        k = st.columns(4)
        k[0].metric("Checked in", len(present),
                    f"{len(present) - prev_n:+d} vs {dt.date.fromisoformat(prev['date']):%d %b}" if prev else None,
                    border=True)
        k[1].metric("First-timers here", len(ft_today), f"{len(new_today)} first visit today" if new_today else None,
                    delta_color="off", border=True)
        k[2].metric("Of the register", f"{len(present) / max(len(active), 1):.0%}", f"{len(active)} people",
                    delta_color="off", border=True)
        k[3].metric("Previous service", prev_n if prev else "–",
                    dt.date.fromisoformat(prev["date"]).strftime("%a %d %b") if prev else None,
                    delta_color="off", border=True)

        if not present:
            st.info(f"No one is checked in for {day:%A %d %B} yet. Ticks on the **Check-in** page appear here "
                    "within seconds.", icon=":material/hourglass_top:")
        else:
            ev = pd.DataFrame([dict(id=mid, name=members.get(mid, {}).get("full_name", "(removed)"),
                                    type=members.get(mid, {}).get("type", "member"), at=at)
                               for mid, at in present.items()])
            ev["time"] = _parse_times(ev["at"]).set_axis(ev.index)  # keep NZ time zone
            ev = ev.sort_values("time")
            c1, c2 = st.columns([3, 2])
            with c1:
                arr = ev.dropna(subset=["time"]).assign(n=1)
                if len(arr):
                    arr["arrived"] = arr["n"].cumsum()
                    fig = px.line(arr, x="time", y="arrived", line_shape="hv", markers=True,
                                  labels={"arrived": "Checked in", "time": ""})
                    fig.update_traces(line=dict(width=2, color=SERIES["members"]), marker=dict(size=8),
                                      hovertemplate="%{x|%H:%M}: %{y} checked in<extra></extra>")
                    _plot(fig, 330, "Arrivals so far")
            with c2, card("live_latest"):
                st.markdown("**Latest arrivals**")
                latest = ev.sort_values("time", ascending=False).head(12)
                st.dataframe(pd.DataFrame({
                    "Time": latest["time"].dt.strftime("%H:%M").fillna(""),
                    "Name": latest["name"],
                    "": latest["type"].map({"first_timer": "✨ First-timer"}).fillna("")}),
                    hide_index=True, width="stretch", height=min(38 * (len(latest) + 1) + 4, 480))
        st.caption(f"Live · updated {dt.datetime.now(TZ):%H:%M:%S} · refreshing every {every}s")

    with st.container(key="live_page"):  # refreshes quietly (see app.py CSS)
        live_body()


@db_safe
def page_insights():
    store = get_store()
    header("Insights", "How attendance is trending — services, first-timers and groups", store)
    if not gate(store):
        return
    demo_note(store)
    members, services = store.list_members(), store.list_services()
    mem = {m["id"]: m for m in members}
    past = sorted([s for s in services if s.get("date", "") <= today().isoformat()], key=lambda s: s["date"])
    if not past:
        st.info("No services recorded yet. After a few Sundays of ticking on **Check-in**, trends appear here.")
        return

    rows = []
    for s in past:
        p = s.get("present") or {}
        rows.append(dict(date=pd.to_datetime(s["date"]), present=len(p),
                         first_timers=sum(1 for mid in p if mem.get(mid, {}).get("type") == "first_timer")))
    per = pd.DataFrame(rows)
    per["members"] = per.present - per.first_timers
    per["avg4"] = per.present.rolling(4, min_periods=1).mean()
    last, prev = per.iloc[-1], (per.iloc[-2] if len(per) > 1 else None)
    recent_ids = set().union(*[set((s.get("present") or {}).keys()) for s in past[-4:]])

    k = st.columns(4)
    k[0].metric("Services recorded", len(per), border=True)
    k[1].metric("This service", int(last.present),
                f"{int(last.present - prev.present):+d} vs previous" if prev is not None else None, border=True)
    k[2].metric("Average (last 4)", f"{per.present.tail(4).mean():.0f}", border=True)
    k[3].metric("Active people", len(recent_ids), "came at least once in the last 4 services",
                delta_color="off", border=True)

    long = per.melt(id_vars="date", value_vars=["members", "first_timers"], var_name="who", value_name="n")
    long["who"] = long.who.map({"members": "Members", "first_timers": "First-timers"})
    fig = px.bar(long, x="date", y="n", color="who", barmode="stack",
                 color_discrete_map={"Members": SERIES["members"], "First-timers": SERIES["first_timers"]},
                 category_orders={"who": ["Members", "First-timers"]}, labels={"n": "People", "date": ""})
    fig.update_traces(marker_line_width=0, hovertemplate="%{x|%d %b %Y}: %{y}<extra>%{fullData.name}</extra>")
    fig.add_scatter(x=per.date, y=per.avg4, mode="lines", name="4-service average",
                    line=dict(color=INK["secondary"], width=2, dash="dot"), hovertemplate="%{y:.1f}<extra>4-service avg</extra>")
    fig.update_layout(bargap=0.25, hovermode="x unified")
    _plot(fig, 360, "Attendance per service")

    c1, c2 = st.columns(2)
    with c1:
        ft = pd.DataFrame([m for m in members if m.get("first_visit")])
        if ft.empty:
            with card("ins_ft_empty"):
                st.markdown("**First-timers per month**")
                st.caption("No first-visit dates yet.")
        else:
            ft["month"] = pd.to_datetime(ft.first_visit).dt.to_period("M").dt.to_timestamp()
            by_m = ft.groupby("month").size().rename("n").reset_index()
            fig = px.bar(by_m, x="month", y="n", labels={"n": "First-timers", "month": ""})
            fig.update_traces(marker_color=SERIES["first_timers"], hovertemplate="%{x|%b %Y}: %{y}<extra></extra>")
            fig.update_layout(bargap=0.3)
            _plot(fig, 300, "First-timers per month")
    with c2:
        fts = [m for m in members if m.get("type") == "first_timer" or m.get("first_visit")]
        came_back = 0
        for m in fts:
            dates = sorted(s["date"] for s in past if m["id"] in (s.get("present") or {}))
            first = m.get("first_visit") or (dates[0] if dates else None)
            if first and any(d > first for d in dates):
                came_back += 1
        with card("ins_ft_back"):
            st.markdown("**Did first-timers come back?**")
            a, b = st.columns(2)
            a.metric("First-timers", len(fts))
            b.metric("Came back at least once", came_back,
                     f"{came_back / len(fts):.0%}" if fts else None, delta_color="off")
            st.caption("Counts anyone with a first-visit date or marked first-timer who was ticked at a later service.")

    grp = {}
    for s in past[-8:]:
        for mid in (s.get("present") or {}):
            g = (mem.get(mid, {}).get("group") or "").strip() or "(no group)"
            grp[g] = grp.get(g, 0) + 1
    if grp:
        gdf = pd.DataFrame({"group": list(grp), "avg": [v / len(past[-8:]) for v in grp.values()]}).sort_values("avg")
        fig = px.bar(gdf, x="avg", y="group", orientation="h", labels={"avg": "Average per service", "group": ""})
        fig.update_traces(marker_color=SERIES["members"], hovertemplate="%{y}: %{x:.1f} per service<extra></extra>")
        _plot(fig, max(220, 36 * len(gdf) + 90), f"Attendance by group (last {len(past[-8:])} services)")



# ---------------------------------------------------------------- email report
def _mail_cfg() -> dict:
    import report as R

    def get(k):
        try:
            return st.secrets.get(k)
        except Exception:
            return None
    return R.mail_config(get)


def send_now_button(store, key: str, label: str = "Email today's report now", full: bool = False):
    """Sends the report straight away to everyone on the list. Returns True if sent."""
    import report as R
    cfg = _mail_cfg()
    if st.button(label, key=key, icon=":material/forward_to_inbox:", type="primary" if full else "secondary",
                 width="stretch", disabled=store.demo or not cfg["ready"],
                 help=None if cfg["ready"] else "Add brevo_api_key in the app's Secrets first (see Reports)."):
        with st.spinner("Sending…"):
            try:
                out = R.send(store, cfg, kind="manual")
                st.toast(f"Report sent to {', '.join(out['to'])}", icon=":material/mark_email_read:")
                return True
            except Exception as e:
                st.error(f"Couldn't send the email: {e}", icon=":material/error:")
    return False


REPORT_SETUP = """
**One-time setup (about 5 minutes)** — the report is sent through **Brevo**, a free email service (300 emails a day).

1. **Brevo account** — go to **brevo.com** → *Sign up free* using the church email (greaterloveauckland@gmail.com)
   and confirm the email Brevo sends you. That address becomes the verified *sender*.
2. **API key** — in Brevo, click your name (top right) → **SMTP & API** → **API Keys** tab → **Generate a new API key**,
   name it *FCC Attendance*, and copy it (it starts with `xkeysib-`).
3. **This app** — share.streamlit.io → *fcc-attendance* → ⋮ → **Settings → Secrets**, add:
   ```toml
   brevo_api_key = "xkeysib-…"
   report_sender = "greaterloveauckland@gmail.com"
   ```
4. **The 1pm schedule** — github.com/Kellyzicoder/fcc-attendance → **Settings → Secrets and variables → Actions →
   New repository secret**, add: `DATABASE_URL` (same as in the app's Secrets), `BREVO_API_KEY`, `REPORT_SENDER`.

Then press **Send report now** above to test. The first one may land in *Spam* — mark it *Not spam* once.
The daily email goes out at about 1pm NZ time; if it ever fails, GitHub emails the repo owner.
"""


@db_safe
def page_reports():
    import report as R
    store = get_store()
    header("Reports", "The 1pm email to church leaders — who gets it, what's in it, and send it now", store)
    if not gate(store, hq=True):
        return
    demo_note(store)
    cfg = _mail_cfg()
    left, right = st.columns([1, 1.4], gap="medium")
    with left:
        with card("rep_send"):
            st.markdown("**Send the report**")
            st.caption("Goes out automatically every day at about 1pm (NZ). Use this to send the latest numbers any time — "
                       "e.g. straight after the service, before 6pm.")
            if not cfg["ready"]:
                st.warning("Email isn't set up yet — see the steps below.", icon=":material/settings:")
            send_now_button(store, "rep_send_now", "Send report now", full=True)
        with card("rep_to"):
            st.markdown("**Who gets it**")
            current = R.recipients(store)
            text = st.text_area("Email addresses (one per line)", "\n".join(current), height=130, key="rep_to_text",
                                disabled=store.demo)
            if st.button("Save recipients", key="rep_save", icon=":material/save:", disabled=store.demo):
                good, bad = R.save_recipients(store, text)
                if bad:
                    st.error("Not saved — these don't look like email addresses: " + ", ".join(bad))
                elif good:
                    st.success(f"Saved {len(good)} recipient{'s' if len(good) != 1 else ''}.")
                else:
                    st.error("Add at least one email address.")
        with card("rep_log"):
            st.markdown("**Recent emails**")
            hist = store.email_history(10)
            if not hist:
                st.caption("Nothing sent yet.")
            else:
                st.dataframe(pd.DataFrame({
                    "Sent": _parse_times([h["sent_at"] for h in hist]).dt.strftime("%a %d %b %H:%M"),
                    "Type": ["1pm (automatic)" if h["kind"] == "daily" else "Sent from the app" for h in hist],
                    "": ["✅ Sent" if h["ok"] else "❌ Failed" for h in hist],
                    "Details": [h["detail"] for h in hist]}), hide_index=True, width="stretch")
        with st.expander("Set up email sending", icon=":material/settings:", expanded=not cfg["ready"]):
            st.markdown(REPORT_SETUP)
    with right, card("rep_preview"):
        r = R.build(store)
        st.markdown(f"**Preview** · {r['subject']}")
        import streamlit.components.v1 as components
        components.html(r["html"], height=900, scrolling=True)
        st.download_button("Download the Excel attachment", r["xlsx"], r["filename"],
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           icon=":material/table_view:")


REGISTRATIONS_SQL = """-- Sign-ups from the FCC welcome form. Safe to run more than once.
create table if not exists registrations (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now()
);
alter table registrations
  add column if not exists full_name     text,
  add column if not exists phone         text,
  add column if not exists email         text,
  add column if not exists invited_by    text,
  add column if not exists first_visit   date,
  add column if not exists notes         text,
  add column if not exists wants_contact boolean default true,
  add column if not exists status        text default 'pending',
  add column if not exists member_id     text,
  add column if not exists created_at    timestamptz default now();

alter table registrations enable row level security;

-- The public form can only ADD pending sign-ups. It can't read, change or delete anything.
drop policy if exists "form can submit" on registrations;
create policy "form can submit" on registrations
  for insert to anon
  with check (status = 'pending' and length(trim(full_name)) between 2 and 120);
grant insert on registrations to anon;"""


SETUP_GUIDE = """
**Connect a Postgres database** — Supabase (recommended: login, table editor, SQL editor) or Neon. Both have free plans.

**Supabase**
1. Sign up at **supabase.com** → *New project* → choose a region near you (e.g. Sydney) and a database password.
2. Click **Connect** (top of the project) → *Connection string* → **Session pooler** → copy the URI
   (it looks like `postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-….pooler.supabase.com:5432/postgres`)
   and put your database password in place of `[YOUR-PASSWORD]`.
   Free Supabase projects pause after 7 days with no activity — weekly check-ins keep it awake; if it pauses,
   click *Restore* in Supabase.

**Neon** (alternative)
1. Sign up at **neon.tech** (or *Vercel → Storage → Neon*) → create a project → copy the connection string.

**Then, on share.streamlit.io** → your app → ⋮ → *Settings → Secrets*, paste:

```toml
attendance_password = "password-for-the-team"
admin_password = "a-different-password-for-admins"
database_url = "postgresql://…your connection string…?sslmode=require"
```
Save — the app restarts, creates its three tables, and switches from demo to your database.
Then open **Members → Import CSV** and upload your two sheets.

Optional, for the SQL page: create a read-only database user and add its URI as `database_url_readonly`.
The SQL page already runs every query in a read-only transaction, so it cannot change data either way.

You can also query the same tables in Supabase's own **SQL editor**, or from Python on your laptop:

```python
import pandas as pd, psycopg
with psycopg.connect("postgresql://…") as conn:
    df = pd.read_sql("SELECT * FROM attendance", conn)
```

**Welcome form sign-ups** — run the SQL in *Members → Sign-ups* once in Supabase's SQL editor. It creates the
`registrations` table the public form writes to, locked down so the form can only add new sign-ups.

Never commit connection strings or your CSVs to GitHub — the repo is public. Both are blocked in `.gitignore`.
"""


EXAMPLES = {
    "Attendance per service": """SELECT s.service_date, s.name, COUNT(a.member_id) AS present
FROM services s
LEFT JOIN attendance a ON a.service_date = s.service_date
GROUP BY s.service_date, s.name
ORDER BY s.service_date DESC""",
    "Missed in a row (who to call)": """WITH last_seen AS (
  SELECT m.id, m.full_name, m.phone, MAX(a.service_date) AS last_seen
  FROM members m
  LEFT JOIN attendance a ON a.member_id = m.id
  GROUP BY m.id, m.full_name, m.phone
)
SELECT l.full_name, l.phone, l.last_seen,
       (SELECT COUNT(*) FROM services s
        WHERE s.service_date > COALESCE(l.last_seen, '1900-01-01')
          AND s.service_date <= CURRENT_DATE) AS missed_in_a_row
FROM last_seen l
ORDER BY missed_in_a_row DESC, l.full_name""",
    "Attendance rate per person": """SELECT m.full_name, m.group_name,
       COUNT(a.member_id) AS attended,
       (SELECT COUNT(*) FROM services) AS services,
       ROUND(100.0 * COUNT(a.member_id) / NULLIF((SELECT COUNT(*) FROM services), 0), 1) AS rate_pct
FROM members m
LEFT JOIN attendance a ON a.member_id = m.id
GROUP BY m.id, m.full_name, m.group_name
ORDER BY rate_pct DESC""",
    "First-timers: did they come back?": """SELECT m.full_name, m.first_visit, m.invited_by,
       COUNT(a.member_id) AS visits, MAX(a.service_date) AS last_seen
FROM members m
LEFT JOIN attendance a ON a.member_id = m.id
WHERE m.type = 'first_timer'
GROUP BY m.id, m.full_name, m.first_visit, m.invited_by
ORDER BY m.first_visit DESC""",
    "Attendance by group": """SELECT COALESCE(NULLIF(m.group_name, ''), '(no group)') AS grp,
       COUNT(DISTINCT m.id) AS people, COUNT(a.member_id) AS check_ins
FROM members m
LEFT JOIN attendance a ON a.member_id = m.id
GROUP BY 1
ORDER BY check_ins DESC""",
    "Who invited the most first-timers": """SELECT invited_by, COUNT(*) AS first_timers
FROM members
WHERE invited_by IS NOT NULL AND invited_by <> ''
GROUP BY invited_by
ORDER BY first_timers DESC""",
}


@db_safe
def page_sql():
    store = get_store()
    header("SQL", "Ask the database anything — read-only, so nothing can be changed from here", store)
    if not gate(store, hq=True):
        return
    demo_note(store)
    mode, _ = layout_prefs()
    left, right = (st.container(), st.container()) if mode == "Stacked" else st.columns([2, 5])
    with left, card("sql_tables"):
        st.markdown("**Tables**")
        st.code("members\n  id, full_name, phone,\n  email, group_name, role,\n  status, type,\n  date_joined, first_visit,\n"
                "  invited_by, follow_up\n\nservices\n  service_date, name\n\nattendance\n"
                "  service_date,\n  member_id, checked_at", language=None)
        pick = st.selectbox("Example queries", list(EXAMPLES), index=None, placeholder="Pick an example…")
        if pick and st.session_state.get("sql_pick") != pick:
            st.session_state.sql_pick = pick
            st.session_state.sql_text = EXAMPLES[pick]
    with right:
        st.session_state.setdefault("sql_text", EXAMPLES["Missed in a row (who to call)"])
        sql = st.text_area("SQL", key="sql_text", height=230, label_visibility="collapsed")
        run = st.button("Run query", type="primary", icon=":material/play_arrow:")
        if run or "sql_result" not in st.session_state:
            try:
                t0 = dt.datetime.now()
                df = store.run_query(sql)
                st.session_state.sql_result = (df, (dt.datetime.now() - t0).total_seconds(), None)
            except Exception as e:  # show the database's own error message
                st.session_state.sql_result = (None, 0, str(e).strip().splitlines()[0][:400])
        df, secs, err = st.session_state.sql_result
        if err:
            st.error(err, icon=":material/error:")
        elif df is not None:
            with card("sql_result"):
                st.caption(f"{len(df):,} rows · {secs * 1000:.0f} ms" + (" · first 5,000 shown" if len(df) >= 5000 else ""))
                st.dataframe(df, hide_index=True, width="stretch", height=min(38 * (len(df) + 1) + 4, 520))
                st.download_button("Download results (CSV)", df.to_csv(index=False), "query_results.csv", "text/csv",
                                   icon=":material/download:")
