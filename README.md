# SkyHold

Free, self-hosted weather-hold status for outdoor trades. Mark the morning Hold / Proceed / All-clear, share one magic link, and notify customer and crew together. Optional weather suggestion — you always confirm.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Create a weather-sensitive job (customer, crew contacts, scheduled date/window, site label, trade)
- Morning board for **today** with big Hold / Proceed / All-clear actions
- Share an unguessable public link `{PUBLIC_BASE_URL}/w/{token}` — Copy link, Copy blurb
- Customer and crew see status badge, hold reason, new date/window, and a short decision timeline
- Optional BYO SMTP email and/or Twilio SMS — one template to customer + crew (deduped)
- Optional BYO OpenWeather key for a **suggestion** banner only (never auto-holds)
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false,"sms_configured":false,"weather_configured":false}` even when SMTP/Twilio/weather unset

Without SMTP, Twilio, or weather you still get in-app status and can copy the link / blurb. Product is fully usable via Copy link alone.

## Privacy

Self-hosted. You run the box; the owner is the data controller for customer and crew contact fields. No Stripe, no bundled SMS numbers, no third-party analytics SaaS. Data lives in your SQLite file on the Compose volume. Weather lat/lon (if set) are only used for the advisory banner.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/skyhold.git
cd skyhold
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, and `OWNER_PASSWORD`. Leave `SMTP_*`, Twilio, OpenWeather, and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `skyhold-data` volume at `/data/skyhold.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
PUBLIC_BASE_URL=http://localhost:8080
BUSINESS_NAME=Harbor Roofing
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*`, Twilio, and weather vars unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, `"sms_configured":false`, `"weather_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`, create a job scheduled **today** (e.g. Smith residence — roof dry-in / Jordan Smith). Copy the `/w/{token}` link from the detail page.

3. Open the public link (second browser or phone). Confirm business name, job title, badge **Scheduled**, and a created timeline entry.

4. On the morning board or detail page, hit **Hold** with reason **High winds** and a new date. Refresh the public page — On hold, reason, new date, and a hold event appear.

5. Hit **All-clear** — public shows All clear; timeline keeps the hold event and appends all-clear (history kept).

See `sample-job.md` for example field values.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/skyhold.db`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Public page. |
| `BUSINESS_PHONE` | Optional phone shown under "Questions? Call us." |
| `PUBLIC_BASE_URL` | No trailing slash. Used in magic links, e.g. `http://localhost:8080`. |
| `FROM_NAME`, `FROM_EMAIL` | SMTP From / email sign-off. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional email notify. If `SMTP_HOST` is unset, email notify is unused. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Optional SMS notify. If unset, SMS is unused. |
| `OPENWEATHER_API_KEY` or `WEATHER_API_KEY` | Optional suggestion banner. |
| `WEATHER_LAT`, `WEATHER_LON` | Required with weather key for the banner. |
| `WEATHER_PRECIP_THRESHOLD` | Default 50 (percent). |
| `WEATHER_WIND_THRESHOLD` | Optional wind mph threshold. |
| `MARKETING_URL` | If set, footer link **Powered by SkyHold** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP / Twilio / weather secrets and `OWNER_PASSWORD` are never written to application logs.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false,"sms_configured":false,"weather_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. `sms_configured` is `true` only when all three Twilio vars are set. `weather_configured` is `true` only when a weather API key and lat/lon are set. Health succeeds even when all are unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./skyhold.db
export OWNER_PASSWORD=testpass
export PUBLIC_BASE_URL=http://localhost:8080
export BUSINESS_NAME="Harbor Roofing"
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

```bash
python -m unittest test_app.py -v
```

## What this is not

SkyHold is **not** a full dispatch board (no drag-drop calendar, no tech GPS). It is **not** an auto-reschedule platform (weather never writes job state). It is **not** Nudge (no Day 0/3/7 drip). It is **not** AfterJob (no CSAT / Google review ask). It is **not** FormFirst (no contact-form webhook). It is **not** OpenPing (no quote open-tracking). It is **not** ChangeSlip (no priced Accept). It is **not** PartPing (no parts milestones).

No maps / live radar embed, no Stripe / payments, no Redis, no Celery, no LLM, no second Compose service.
