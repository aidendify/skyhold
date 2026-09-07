"""SkyHold: hosted weather-hold status for outdoor trades."""

from __future__ import annotations

import secrets
from datetime import datetime

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import helpers as H

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.secret_key = __import__("os").environ.get("SECRET_KEY", "skyhold-self-hosted-change-me")

app.teardown_appcontext(H.close_db)


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H._env("MARKETING_URL"),
        "smtp_configured": H.smtp_configured(),
        "sms_configured": H.sms_configured(),
        "weather_configured": H.weather_configured(),
        "business_name": H.business_name(),
        "business_phone": H.business_phone(),
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "state_label": H.state_label,
        "event_label": H.event_label,
        "format_date_display": H.format_date_display,
        "first_name": H.first_name,
        "TRADES": H.TRADES,
        "STATE_LABELS": H.STATE_LABELS,
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


def _parse_date(raw: str | None, field: str, errors: list[str]) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        errors.append(f"{field} must be YYYY-MM-DD.")
        return None


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "smtp_configured": H.smtp_configured(),
            "sms_configured": H.sms_configured(),
            "weather_configured": H.weather_configured(),
        }
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    today = H.board_today()
    db = H.get_db()
    today_jobs = db.execute(
        """
        SELECT * FROM jobs
        WHERE weather_sensitive = 1 AND scheduled_date = ?
        ORDER BY id ASC
        """,
        (today,),
    ).fetchall()
    upcoming = db.execute(
        """
        SELECT * FROM jobs
        WHERE weather_sensitive = 1 AND scheduled_date > ?
        ORDER BY scheduled_date ASC, id ASC
        LIMIT 20
        """,
        (today,),
    ).fetchall()
    weather = H.fetch_weather_suggestion()
    return render_template(
        "index.html",
        today=today,
        today_jobs=today_jobs,
        upcoming=upcoming,
        weather=weather,
    )


@app.get("/jobs")
def jobs_list():
    today = H.board_today()
    rows = H.get_db().execute(
        """
        SELECT * FROM jobs
        WHERE weather_sensitive = 1
        ORDER BY scheduled_date ASC, id ASC
        """
    ).fetchall()
    return render_template("jobs.html", jobs=rows, today=today)


@app.route("/jobs/new", methods=["GET", "POST"])
def new_job():
    if request.method == "GET":
        return render_template(
            "new_job.html",
            default_date=H.board_today(),
            form=None,
        )

    job_name = (request.form.get("job_name") or "").strip()
    customer_name = (request.form.get("customer_name") or "").strip()
    customer_email = (request.form.get("customer_email") or "").strip() or None
    customer_phone = (request.form.get("customer_phone") or "").strip() or None
    crew_emails = (request.form.get("crew_emails") or "").strip() or None
    crew_phones = (request.form.get("crew_phones") or "").strip() or None
    job_ref = (request.form.get("job_ref") or "").strip() or None
    site_label = (request.form.get("site_label") or "").strip() or None
    scheduled_window = (request.form.get("scheduled_window") or "").strip() or None
    trade = (request.form.get("trade") or "").strip() or None
    notes = (request.form.get("notes") or "").strip() or None
    weather_sensitive = 1 if request.form.get("weather_sensitive") != "0" else 0

    errors: list[str] = []
    if not job_name:
        errors.append("Job name is required.")
    if not customer_name:
        errors.append("Customer name is required.")
    scheduled_date = _parse_date(request.form.get("scheduled_date"), "Scheduled date", errors)
    if not scheduled_date:
        if not (request.form.get("scheduled_date") or "").strip():
            errors.append("Scheduled date is required.")
    if trade and trade not in H.TRADES:
        errors.append("Unknown trade.")

    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(
            "new_job.html",
            default_date=H.board_today(),
            form=request.form,
        ), 400

    token = secrets.token_hex(32)
    now = H.utc_now_iso()
    db = H.get_db()
    cur = db.execute(
        """
        INSERT INTO jobs (
            token, job_name, customer_name, customer_email, customer_phone,
            crew_emails, crew_phones, job_ref, site_label,
            scheduled_date, scheduled_window, trade, notes, weather_sensitive,
            state, reason, new_date, new_window, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', NULL, NULL, NULL, ?, ?)
        """,
        (
            token,
            job_name,
            customer_name,
            customer_email,
            customer_phone,
            crew_emails,
            crew_phones,
            job_ref,
            site_label,
            scheduled_date,
            scheduled_window,
            trade,
            notes,
            weather_sensitive,
            now,
            now,
        ),
    )
    job_id = cur.lastrowid
    H.add_event(db, job_id, "created", state="scheduled")
    db.commit()
    flash("Weather-sensitive job created.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/jobs/<int:job_id>")
def job_detail(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    return render_template(
        "job_detail.html",
        job=job,
        events=H.list_events(job_id),
        notify_log=H.list_notify_log(job_id),
        public_url=H.public_status_url(job["token"]),
        blurb=H.copy_blurb(job),
        can_notify=H.has_notify_destination(job),
        notify_default=H.has_notify_destination(job),
    )


def _apply_decision(
    job_id: int,
    *,
    kind: str,
    new_state: str,
    reason: str | None = None,
    new_date: str | None = None,
    new_window: str | None = None,
    require_reason: bool = False,
) -> None:
    job = H.get_job(job_id)
    if job is None:
        abort(404)

    errors: list[str] = []
    if require_reason and not (reason or "").strip():
        errors.append("Reason is required for Hold.")
    parsed_date = None
    if new_date is not None and new_date != "":
        parsed_date = _parse_date(new_date, "New date", errors)
    elif new_date == "":
        parsed_date = None

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("job_detail", job_id=job_id))

    notify_flag = request.form.get("notify") == "1"
    now = H.utc_now_iso()
    reason_val = (reason or "").strip() or None
    window_val = (new_window or "").strip() or None

    if kind == "proceed":
        reason_store = None
        date_store = None
        window_store = None
    elif kind == "hold":
        reason_store = reason_val
        date_store = parsed_date
        window_store = window_val
    else:
        reason_store = reason_val
        date_store = parsed_date if parsed_date is not None else job["new_date"]
        window_store = window_val if window_val is not None else job["new_window"]
        if "new_date" in request.form and not (request.form.get("new_date") or "").strip():
            if (request.form.get("new_date") or "").strip() == "" and "new_date" in request.form:
                date_store = parsed_date if parsed_date else job["new_date"]

    db = H.get_db()
    db.execute(
        """
        UPDATE jobs SET
            state = ?, reason = ?, new_date = ?, new_window = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_state, reason_store, date_store, window_store, now, job_id),
    )
    event_id = H.add_event(
        db,
        job_id,
        kind,
        state=new_state,
        reason=reason_store,
        new_date=date_store,
        new_window=window_store,
        at=now,
    )
    db.commit()

    job = H.get_job(job_id)
    notify_failed = False
    if notify_flag and H.has_notify_destination(job):
        attempted, ok, _errs = H.notify_customer_and_crew(app, job, event_id=event_id)
        db.commit()
        if attempted and not ok:
            notify_failed = True

    if notify_failed:
        flash("Saved; notify failed.", "error")
    else:
        flash(f"{H.state_label(new_state)} recorded.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/hold")
def hold_job(job_id: int):
    return _apply_decision(
        job_id,
        kind="hold",
        new_state="on_hold",
        reason=request.form.get("reason"),
        new_date=request.form.get("new_date"),
        new_window=request.form.get("new_window"),
        require_reason=True,
    )


@app.post("/jobs/<int:job_id>/proceed")
def proceed_job(job_id: int):
    return _apply_decision(
        job_id,
        kind="proceed",
        new_state="proceed",
        reason=None,
        new_date=None,
        new_window=None,
        require_reason=False,
    )


@app.post("/jobs/<int:job_id>/all-clear")
def all_clear_job(job_id: int):
    return _apply_decision(
        job_id,
        kind="all_clear",
        new_state="all_clear",
        reason=request.form.get("note") or request.form.get("reason"),
        new_date=request.form.get("new_date"),
        new_window=request.form.get("new_window"),
        require_reason=False,
    )


@app.get("/w/<token>")
def public_status(token: str):
    job = H.get_job_by_token(token)
    if job is None:
        abort(404)
    events = H.list_events(job["id"])
    public_events = [e for e in events if e["kind"] in ("created", "hold", "proceed", "all_clear")]
    return render_template(
        "public_status.html",
        job=job,
        events=public_events,
        public=True,
    )


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html", public=True), 404


get_job = H.get_job
get_job_by_token = H.get_job_by_token
get_db = H.get_db
list_events = H.list_events

init_db()


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
