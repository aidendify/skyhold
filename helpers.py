"""SkyHold helpers: env, db, mail, SMS, weather suggestion, blurbs."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from flask import g

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = APP_ROOT / "skyhold.db"

OPEN_ENDPOINTS = {
    "health",
    "login",
    "logout",
    "public_status",
    "static",
}

STATES = ("scheduled", "on_hold", "proceed", "all_clear")

STATE_LABELS = {
    "scheduled": "Scheduled",
    "on_hold": "On hold",
    "proceed": "Proceeding",
    "all_clear": "All clear",
}

TRADES = (
    "roofing",
    "concrete",
    "paint",
    "siding",
    "landscaping",
    "solar",
    "other",
)

EVENT_LABELS = {
    "created": "Created",
    "hold": "Hold",
    "proceed": "Proceed",
    "all_clear": "All clear",
    "note": "Note",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    raw = _env("DATABASE_PATH")
    return raw if raw else str(DEFAULT_DB)


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def sms_configured() -> bool:
    return bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_FROM_NUMBER")
    )


def weather_api_key() -> str:
    return _env("OPENWEATHER_API_KEY") or _env("WEATHER_API_KEY")


def weather_configured() -> bool:
    return bool(weather_api_key() and _env("WEATHER_LAT") and _env("WEATHER_LON"))


def owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD", "").strip()


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def business_name() -> str:
    return _env("BUSINESS_NAME") or "SkyHold"


def business_phone() -> str:
    return _env("BUSINESS_PHONE")


def board_today() -> str:
    """YYYY-MM-DD for the morning board. Override with SKYHOLD_TODAY for tests."""
    override = _env("SKYHOLD_TODAY")
    if override:
        return override
    return date.today().isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_iso(utc_now())


def public_status_url(token: str) -> str:
    base = public_base_url() or "http://localhost:8080"
    return f"{base}/w/{token}"


def state_label(key: str) -> str:
    return STATE_LABELS.get(key, key)


def event_label(kind: str) -> str:
    return EVENT_LABELS.get(kind, kind)


def first_name(full: str) -> str:
    parts = (full or "").strip().split()
    return parts[0] if parts else ""


def format_date_display(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    except ValueError:
        return raw


def connect_db() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = connect_db()
        g._db = db
    return db


def close_db(_exc: BaseException | None = None) -> None:
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            job_name TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            customer_email TEXT,
            customer_phone TEXT,
            crew_emails TEXT,
            crew_phones TEXT,
            job_ref TEXT,
            site_label TEXT,
            scheduled_date TEXT NOT NULL,
            scheduled_window TEXT,
            trade TEXT,
            notes TEXT,
            weather_sensitive INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL,
            reason TEXT,
            new_date TEXT,
            new_window TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            kind TEXT NOT NULL,
            state TEXT,
            reason TEXT,
            new_date TEXT,
            new_window TEXT,
            at TEXT NOT NULL,
            meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS notify_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            event_id INTEGER,
            channel TEXT NOT NULL,
            destination TEXT NOT NULL,
            ok INTEGER NOT NULL,
            error TEXT,
            at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_token ON jobs(token);
        CREATE INDEX IF NOT EXISTS idx_jobs_scheduled_date ON jobs(scheduled_date);
        CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id);
        CREATE INDEX IF NOT EXISTS idx_notify_log_job_id ON notify_log(job_id);
        """
    )
    db.commit()


def get_job(job_id: int):
    return get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_by_token(token: str):
    return get_db().execute("SELECT * FROM jobs WHERE token = ?", (token,)).fetchone()


def list_events(job_id: int):
    return get_db().execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY id ASC", (job_id,)
    ).fetchall()


def list_notify_log(job_id: int):
    return get_db().execute(
        "SELECT * FROM notify_log WHERE job_id = ? ORDER BY id DESC", (job_id,)
    ).fetchall()


def add_event(
    conn,
    job_id: int,
    kind: str,
    *,
    state: str | None = None,
    reason: str | None = None,
    new_date: str | None = None,
    new_window: str | None = None,
    meta=None,
    at: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO events (job_id, kind, state, reason, new_date, new_window, at, meta_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            kind,
            state,
            reason,
            new_date,
            new_window,
            at or utc_now_iso(),
            json.dumps(meta) if meta is not None else None,
        ),
    )
    return int(cur.lastrowid)


def mask_destination(dest: str, channel: str) -> str:
    raw = (dest or "").strip()
    if not raw:
        return "***"
    if channel == "email":
        if "@" in raw:
            local, _, domain = raw.partition("@")
            keep = local[:2] if len(local) > 2 else local[:1]
            return f"{keep}***@{domain}"
        return "***"
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"


def parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def collect_emails(job) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for e in [job["customer_email"], *parse_csv_list(job["crew_emails"])]:
        if not e:
            continue
        item = e.strip()
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        emails.append(item)
    return emails


def collect_phones(job) -> list[str]:
    phones: list[str] = []
    seen: set[str] = set()
    for p in [job["customer_phone"], *parse_csv_list(job["crew_phones"])]:
        if not p:
            continue
        item = p.strip()
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        phones.append(item)
    return phones


def has_notify_destination(job) -> bool:
    return bool(collect_emails(job) or collect_phones(job))


def can_notify_email(job) -> bool:
    return smtp_configured() and bool(collect_emails(job))


def can_notify_sms(job) -> bool:
    return sms_configured() and bool(collect_phones(job))


def send_smtp(to_email: str, subject: str, body: str) -> None:
    host = _env("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured.")
    from_email = _env("FROM_EMAIL")
    if not from_email:
        raise RuntimeError("FROM_EMAIL is required to send mail.")
    port = int(_env("SMTP_PORT") or "587")
    user = _env("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD", "")
    tls_raw = _env("SMTP_TLS") or "true"
    use_tls = tls_raw.lower() in {"1", "true", "yes", "on"}
    from_name = _env("FROM_NAME")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = to_email
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def send_twilio_sms(to_phone: str, body: str) -> None:
    sid = _env("TWILIO_ACCOUNT_SID")
    token = _env("TWILIO_AUTH_TOKEN")
    from_number = _env("TWILIO_FROM_NUMBER")
    if not (sid and token and from_number):
        raise RuntimeError("Twilio is not configured.")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode(
        {"To": to_phone, "From": from_number, "Body": body}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    credentials = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Twilio HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Twilio HTTP {exc.code}") from exc


def decision_subject(job) -> str:
    return f"Weather update: {job['job_name']}"


def decision_body(job) -> str:
    business = business_name()
    link = public_status_url(job["token"])
    label = state_label(job["state"])
    lines = [
        f"Weather update from {business}",
        "",
        f"Job: {job['job_name']}",
        f"Status: {label}",
        f"Scheduled: {format_date_display(job['scheduled_date'])}",
    ]
    if job["scheduled_window"]:
        lines.append(f"Window: {job['scheduled_window']}")
    if job["state"] == "on_hold":
        if job["reason"]:
            lines.append(f"Reason: {job['reason']}")
        if job["new_date"]:
            lines.append(f"New date: {format_date_display(job['new_date'])}")
        if job["new_window"]:
            lines.append(f"New window: {job['new_window']}")
    if job["state"] == "all_clear" and job["new_date"]:
        lines.append(f"Confirmed date: {format_date_display(job['new_date'])}")
    lines.extend(["", f"Status page: {link}", "", f"- {business}"])
    return "\n".join(lines)


def decision_sms_body(job) -> str:
    link = public_status_url(job["token"])
    label = state_label(job["state"])
    return f"{business_name()}: {job['job_name']} — {label}. {link}"


def copy_blurb(job) -> str:
    """Short blurb for Copy blurb (SMS/text style)."""
    return decision_sms_body(job)


def log_notify(
    conn,
    job_id: int,
    channel: str,
    destination: str,
    ok: bool,
    error: str | None = None,
    event_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO notify_log (job_id, event_id, channel, destination, ok, error, at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            event_id,
            channel,
            mask_destination(destination, channel),
            1 if ok else 0,
            error,
            utc_now_iso(),
        ),
    )


def notify_customer_and_crew(app, job, event_id: int | None = None) -> tuple[bool, bool, list[str]]:
    """Send one template to customer + crew. Failures do not raise.
    Returns (any_attempted, all_ok, channel_errors).
    """
    errors: list[str] = []
    attempted = False
    db = get_db()
    subject = decision_subject(job)
    email_body = decision_body(job)
    sms_body = decision_sms_body(job)

    if smtp_configured():
        for to_email in collect_emails(job):
            attempted = True
            try:
                send_smtp(to_email, subject, email_body)
                log_notify(db, job["id"], "email", to_email, True, event_id=event_id)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning(
                    "Email notify failed for job_id=%s: %s", job["id"], type(exc).__name__
                )
                log_notify(
                    db,
                    job["id"],
                    "email",
                    to_email,
                    False,
                    error=type(exc).__name__,
                    event_id=event_id,
                )
                errors.append("email")

    if sms_configured():
        for to_phone in collect_phones(job):
            attempted = True
            try:
                send_twilio_sms(to_phone, sms_body)
                log_notify(db, job["id"], "sms", to_phone, True, event_id=event_id)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning(
                    "SMS notify failed for job_id=%s: %s", job["id"], type(exc).__name__
                )
                log_notify(
                    db,
                    job["id"],
                    "sms",
                    to_phone,
                    False,
                    error=type(exc).__name__,
                    event_id=event_id,
                )
                errors.append("sms")

    all_ok = attempted and not errors
    return attempted, (not errors) if attempted else True, errors


def fetch_weather_suggestion() -> dict | None:
    """Advisory-only OpenWeather check. Never writes job state.
    Returns None if unset or on any failure.
    """
    if not weather_configured():
        return None
    api_key = weather_api_key()
    lat = _env("WEATHER_LAT")
    lon = _env("WEATHER_LON")
    precip_threshold = float(_env("WEATHER_PRECIP_THRESHOLD") or "50")
    wind_raw = _env("WEATHER_WIND_THRESHOLD")
    wind_threshold = float(wind_raw) if wind_raw else None

    params = urllib.parse.urlencode(
        {
            "lat": lat,
            "lon": lon,
            "appid": api_key,
            "units": "imperial",
        }
    )
    url = f"https://api.openweathermap.org/data/2.5/weather?{params}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status >= 400:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    pop_pct = None
    if "pop" in payload:
        try:
            pop_pct = float(payload["pop"]) * 100
        except (TypeError, ValueError):
            pop_pct = None
    rain = payload.get("rain") or {}
    if pop_pct is None and rain:
        try:
            vol = float(rain.get("1h") or rain.get("3h") or 0)
            pop_pct = 80.0 if vol > 0 else 0.0
        except (TypeError, ValueError):
            pop_pct = None

    wind_mph = None
    wind = payload.get("wind") or {}
    if "speed" in wind:
        try:
            wind_mph = float(wind["speed"])
        except (TypeError, ValueError):
            wind_mph = None

    suggest_hold = False
    reasons: list[str] = []
    if pop_pct is not None and pop_pct >= precip_threshold:
        suggest_hold = True
        reasons.append(f"precip ~{int(pop_pct)}%")
    if wind_threshold is not None and wind_mph is not None and wind_mph >= wind_threshold:
        suggest_hold = True
        reasons.append(f"wind ~{int(wind_mph)} mph")

    desc = ""
    weather_list = payload.get("weather") or []
    if weather_list:
        desc = (weather_list[0].get("description") or "").strip()

    if suggest_hold:
        detail = ", ".join(reasons) if reasons else "conditions elevated"
        msg = f"Local forecast: {desc or 'elevated weather'} ({detail}) — suggestion: Hold"
    else:
        bits = []
        if desc:
            bits.append(desc)
        if pop_pct is not None:
            bits.append(f"precip ~{int(pop_pct)}%")
        if wind_mph is not None:
            bits.append(f"wind ~{int(wind_mph)} mph")
        detail = ", ".join(bits) if bits else "within thresholds"
        msg = f"Local forecast: {detail} — suggestion: Proceed / All-clear OK"

    return {
        "suggest_hold": suggest_hold,
        "message": msg,
        "precip_pct": pop_pct,
        "wind_mph": wind_mph,
    }
