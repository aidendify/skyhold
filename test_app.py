"""Local Flask test client covering PRD section 12."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

# Ensure env before importing app
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["BUSINESS_NAME"] = "Harbor Roofing"
os.environ["BUSINESS_PHONE"] = ""
os.environ["MARKETING_URL"] = ""
os.environ.pop("SMTP_HOST", None)
os.environ.pop("SMTP_PORT", None)
os.environ.pop("SMTP_USER", None)
os.environ.pop("SMTP_PASSWORD", None)
os.environ.pop("FROM_NAME", None)
os.environ.pop("FROM_EMAIL", None)
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_AUTH_TOKEN", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ.pop("OPENWEATHER_API_KEY", None)
os.environ.pop("WEATHER_API_KEY", None)
os.environ.pop("WEATHER_LAT", None)
os.environ.pop("WEATHER_LON", None)
os.environ.pop("SKYHOLD_TODAY", None)


class SkyHoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "test.db")
        os.environ["DATABASE_PATH"] = db_path
        os.environ["OWNER_PASSWORD"] = "testpass"
        os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
        os.environ["BUSINESS_NAME"] = "Harbor Roofing"
        os.environ["MARKETING_URL"] = ""
        os.environ.pop("SMTP_HOST", None)
        os.environ.pop("TWILIO_ACCOUNT_SID", None)
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        os.environ.pop("TWILIO_FROM_NUMBER", None)
        os.environ.pop("OPENWEATHER_API_KEY", None)
        os.environ.pop("WEATHER_API_KEY", None)
        os.environ.pop("WEATHER_LAT", None)
        os.environ.pop("WEATHER_LON", None)
        os.environ.pop("SKYHOLD_TODAY", None)

        import importlib
        import app as app_module
        import helpers as helpers_module

        importlib.reload(helpers_module)
        importlib.reload(app_module)
        self.app_module = app_module
        self.helpers = helpers_module
        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.app.app_context():
            app_module.init_db()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _login(self) -> None:
        r = self.client.post(
            "/login",
            data={"password": "testpass", "next": "/"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

    def _create_job(
        self,
        job_name: str = "Smith residence — roof dry-in",
        customer_name: str = "Jordan Smith",
        scheduled_date: str | None = None,
        **extra,
    ) -> tuple[int, str]:
        self._login()
        data = {
            "job_name": job_name,
            "customer_name": customer_name,
            "customer_email": extra.get("customer_email", "jordan.smith@example.com"),
            "customer_phone": extra.get("customer_phone", "+15550199"),
            "crew_emails": extra.get("crew_emails", "crew1@example.com"),
            "crew_phones": extra.get("crew_phones", ""),
            "job_ref": extra.get("job_ref", "WO-4421"),
            "site_label": extra.get("site_label", "14 Harbor Lane"),
            "scheduled_date": scheduled_date or date.today().isoformat(),
            "scheduled_window": extra.get("scheduled_window", "7am–3pm"),
            "trade": extra.get("trade", "roofing"),
            "notes": extra.get("notes", "Tarps on site"),
            "weather_sensitive": "1",
        }
        r = self.client.post("/jobs/new", data=data, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), msg=r.data[:500])
        loc = r.headers.get("Location", "")
        self.assertIn("/jobs/", loc)
        job_id = int(loc.rstrip("/").split("/")[-1])
        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertIsNotNone(job)
            return job_id, job["token"]

    def test_01_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)
        self.assertIs(data["sms_configured"], False)
        self.assertIs(data["weather_configured"], False)

    def test_02_auth_gate(self) -> None:
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        self.assertIn("/login", r.headers.get("Location", ""))

        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

        job_id, token = self._create_job()
        self.client.get("/logout")
        r = self.client.get(f"/w/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Smith residence", r.data)

        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

        r = self.client.get("/jobs", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_03_create_job_scheduled_today(self) -> None:
        job_id, token = self._create_job()
        r = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"/w/{token}".encode(), r.data)
        self.assertIn(b"Smith residence", r.data)
        self.assertIn(b"Jordan Smith", r.data)
        self.assertIn(b"Scheduled", r.data)
        self.assertIn(b"Copy link", r.data)
        self.assertIn(b"Copy blurb", r.data)

        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["state"], "scheduled")
            events = self.app_module.list_events(job_id)
            kinds = [e["kind"] for e in events]
            self.assertIn("created", kinds)

        self.client.get("/logout")
        r = self.client.get(f"/w/{token}")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode("utf-8")
        self.assertIn("Harbor Roofing", body)
        self.assertIn("Scheduled", body)
        self.assertIn("Jordan", body)

    def test_04_hold_with_reason_and_new_date(self) -> None:
        job_id, token = self._create_job()
        new_date = (date.today() + timedelta(days=2)).isoformat()
        r = self.client.post(
            f"/jobs/{job_id}/hold",
            data={
                "reason": "High winds",
                "new_date": new_date,
                "new_window": "8am–2pm",
                "notify": "0",
            },
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["state"], "on_hold")
            self.assertEqual(job["reason"], "High winds")
            self.assertEqual(job["new_date"], new_date)
            events = self.app_module.list_events(job_id)
            hold_events = [e for e in events if e["kind"] == "hold"]
            self.assertEqual(len(hold_events), 1)
            self.assertEqual(hold_events[0]["reason"], "High winds")

        r = self.client.get(f"/w/{token}")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode("utf-8")
        self.assertIn("On hold", body)
        self.assertIn("High winds", body)
        self.assertIn(new_date[:4], body)
        self.assertIn("Hold", body)

    def test_05_all_clear_appends_history(self) -> None:
        job_id, token = self._create_job()
        new_date = (date.today() + timedelta(days=1)).isoformat()
        self.client.post(
            f"/jobs/{job_id}/hold",
            data={"reason": "High winds", "new_date": new_date, "notify": "0"},
            follow_redirects=False,
        )
        r = self.client.post(
            f"/jobs/{job_id}/all-clear",
            data={"note": "Radar cleared", "new_date": new_date, "notify": "0"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["state"], "all_clear")
            events = self.app_module.list_events(job_id)
            kinds = [e["kind"] for e in events]
            self.assertIn("created", kinds)
            self.assertIn("hold", kinds)
            self.assertIn("all_clear", kinds)
            self.assertGreaterEqual(len(events), 3)

        r = self.client.get(f"/w/{token}")
        body = r.data.decode("utf-8")
        self.assertIn("All clear", body)
        self.assertIn("Hold", body)
        self.assertIn("High winds", body)

    def test_06_proceed_path(self) -> None:
        job_id, token = self._create_job(job_name="Concrete pour — driveway")
        r = self.client.post(
            f"/jobs/{job_id}/proceed",
            data={"notify": "0"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (302, 303))

        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["state"], "proceed")
            events = self.app_module.list_events(job_id)
            self.assertTrue(any(e["kind"] == "proceed" for e in events))

        r = self.client.get(f"/w/{token}")
        body = r.data.decode("utf-8")
        self.assertIn("Proceeding", body)

    def test_07_works_without_integrations_no_weather_banner(self) -> None:
        job_id, token = self._create_job()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode("utf-8")
        self.assertNotIn("suggestion:", body.lower())
        self.assertNotIn("Local forecast:", body)
        self.assertIn("Smith residence", body)
        self.assertIn("Hold", body)
        self.assertIn("Proceed", body)
        self.assertIn("All-clear", body)

        r = self.client.get(f"/w/{token}")
        self.assertEqual(r.status_code, 200)

    def test_08_no_auto_reschedule(self) -> None:
        job_id, token = self._create_job()
        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["state"], "scheduled")
            src = Path(self.app_module.__file__).read_text(encoding="utf-8")
            helpers_src = Path(self.helpers.__file__).read_text(encoding="utf-8")
            combined = src + helpers_src
            self.assertNotIn("celery", combined.lower())
            self.assertNotIn("redis", combined.lower())
            before = job["state"]
            suggestion = self.helpers.fetch_weather_suggestion()
            self.assertIsNone(suggestion)
            job2 = self.app_module.get_job(job_id)
            self.assertEqual(job2["state"], before)

    def test_09_out_of_scope_absent(self) -> None:
        job_id, token = self._create_job()
        self.client.get("/logout")
        r = self.client.get(f"/w/{token}")
        body = r.data.decode("utf-8").lower()
        self.assertNotIn("accept", body)
        self.assertNotIn("stripe", body)
        self.assertNotIn("csat", body)
        self.assertNotIn("milestone", body)
        self.assertNotIn("google.com/maps", body)
        self.assertNotIn("openstreetmap", body)
        self.assertNotIn("iframe", body)

        with self.app.app_context():
            tables = {
                row[0]
                for row in self.app_module.get_db()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
            self.assertIn("jobs", tables)
            self.assertIn("events", tables)
            self.assertIn("notify_log", tables)
            self.assertNotIn("sequences", tables)
            self.assertNotIn("drips", tables)
            self.assertNotIn("parts", tables)

    def test_10_empty_marketing_no_powered_by(self) -> None:
        job_id, token = self._create_job()
        r = self.client.get(f"/jobs/{job_id}")
        self.assertNotIn(b"Powered by SkyHold", r.data)
        self.client.get("/logout")
        r = self.client.get(f"/w/{token}")
        self.assertNotIn(b"Powered by SkyHold", r.data)
        r = self.client.get("/health")
        r = self.client.get("/login")
        self.assertNotIn(b"Powered by SkyHold", r.data)

    def test_11_unknown_token_404(self) -> None:
        r = self.client.get("/w/not-a-real-token")
        self.assertEqual(r.status_code, 404)

    def test_12_hold_requires_reason(self) -> None:
        job_id, _token = self._create_job()
        r = self.client.post(
            f"/jobs/{job_id}/hold",
            data={"reason": "", "notify": "0"},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            job = self.app_module.get_job(job_id)
            self.assertEqual(job["state"], "scheduled")


if __name__ == "__main__":
    unittest.main()
