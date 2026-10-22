#!/usr/bin/env python3
"""
Netatmo -> Zivy obraz
Fetches data from the Netatmo API and OpenAQ, then pushes everything
to Zivy obraz as custom values. Intended to run every 15 minutes.
"""

import copy
import json
import logging
import os
import signal
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _secret(name: str, default: str | None = None) -> str:
    """Read from Docker Swarm secret file if present, fall back to env var or default."""
    path = f"/run/secrets/{name}"
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    if default is not None:
        return os.environ.get(name, default)
    return os.environ[name]


NETATMO_CLIENT_ID     = _secret("NETATMO_CLIENT_ID")
NETATMO_CLIENT_SECRET = _secret("NETATMO_CLIENT_SECRET")
NETATMO_REFRESH_TOKEN = _secret("NETATMO_REFRESH_TOKEN")
ZO_IMPORT_KEY         = _secret("ZO_IMPORT_KEY")
OPENAQ_API_KEY        = _secret("OPENAQ_API_KEY")

LOCATION_LAT    = float(os.environ.get("LOCATION_LAT", "0"))
LOCATION_LON        = float(os.environ.get("LOCATION_LON", "0"))
OPENAQ_LOCATION_ID  = int(os.environ.get("OPENAQ_LOCATION_ID", "4425"))

# MAC -> room name mapping, e.g. "70:ee:50:83:3e:ce=Pracovna,70:ee:50:83:2e:3e=Obyvak"
HOMECOACH_MAP = {
    k.strip(): v.strip()
    for k, v in (
        pair.split("=", 1)
        for pair in os.environ.get("HOMECOACH_MAP", "").split(",")
        if "=" in pair
    )
}

TOKEN_FILE = "/data/netatmo_tokens.json"
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat")

INTERVAL_SEC       = int(os.environ.get("INTERVAL_SEC", "900"))
STALE_AFTER_MIN    = int(os.environ.get("STALE_AFTER_MIN", "30"))
ALERT_REPEAT_HOURS = float(os.environ.get("ALERT_REPEAT_HOURS", "6"))
DISPLAY_TZ         = os.environ.get("DISPLAY_TZ", "Europe/Prague")

# What to push for values we refuse to send as current. "--" overwrites the old value on
# the display; "" pushes an empty value; STALE_SKIP omits the key (old value stays).
# How Zivy obraz handles an empty value is not documented - see README.
STALE_SKIP  = "@skip"
STALE_VALUE = os.environ.get("STALE_VALUE", "--")

# Alert channels (all optional; read like the other secrets). Nothing set -> log only.
NTFY_URL          = _secret("NTFY_URL", "")
NTFY_TOPIC        = _secret("NTFY_TOPIC", "")
NTFY_TOKEN        = _secret("NTFY_TOKEN", "")
ALERT_WEBHOOK_URL = _secret("ALERT_WEBHOOK_URL", "")
CALLMEBOT_PHONE   = _secret("CALLMEBOT_PHONE", "")    # WhatsApp via callmebot.com, e.g. +420123456789
CALLMEBOT_APIKEY  = _secret("CALLMEBOT_APIKEY", "")

HTTP_TIMEOUT      = (5, 15)   # (connect, read) seconds
PUSH_TIMEOUT      = (5, 20)
RETRY_DELAYS      = (5, 15, 30)  # seconds before retry 1..3

_stop = threading.Event()


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _safe_url(url: str) -> str:
    """scheme://host/path - never the query string or credentials."""
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname}{u.path}"


def _redact(text: str) -> str:
    for secret in (NETATMO_CLIENT_SECRET, NETATMO_REFRESH_TOKEN, ZO_IMPORT_KEY, OPENAQ_API_KEY,
                   NTFY_TOKEN, NTFY_TOPIC, ALERT_WEBHOOK_URL, CALLMEBOT_APIKEY, CALLMEBOT_PHONE):
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def _describe(exc: Exception) -> str:
    """One-line, secret-free description of a failure (never str(exc): it may hold the full URL)."""
    parts = [type(exc).__name__]
    resp = getattr(exc, "response", None)
    if resp is not None:
        parts.append(f"status={resp.status_code}")
    url = getattr(exc, "log_url", None)
    if url:
        parts.append(f"url={url}")
    if resp is not None and 400 <= resp.status_code < 500 and resp.status_code != 429:
        body = " ".join(str(resp.text).split())[:200]
        if body:
            parts.append(f"body={body}")
    return _redact(" ".join(parts))


def _fail(what: str, exc: Exception, level: int = logging.ERROR):
    log.log(level, "%s failed: %s", what, _describe(exc))
    log.debug("Traceback for %s", what, exc_info=exc)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def http_request(method: str, url: str, *, log_url: str | None = None, **kwargs):
    """requests.<method> with a default timeout and retries (5xx, 429, network errors).

    4xx other than 429 is raised at once. The raised exception carries `log_url`
    (the URL without query string) for logging.
    """
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            r = getattr(requests, method)(url, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            e.log_url = log_url or _safe_url(url)
            if not _retryable(e) or attempt == len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[attempt]
            log.warning("Retry %d/%d in %ds after %s", attempt + 1, len(RETRY_DELAYS), delay, _describe(e))
            if _stop.wait(delay):  # shutting down - stop retrying
                raise


# ── tokens ────────────────────────────────────────────────────────────────────

def _atomic_write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_tokens() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return {"refresh_token": NETATMO_REFRESH_TOKEN}


def save_tokens(tokens: dict):
    _atomic_write(TOKEN_FILE, json.dumps(tokens))


def refresh_access_token(refresh_token: str) -> dict:
    log.info("Refreshing access token...")
    r = http_request(
        "post",
        "https://api.netatmo.com/oauth2/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     NETATMO_CLIENT_ID,
            "client_secret": NETATMO_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
    )
    return r.json()


def get_access_token() -> str:
    tokens = load_tokens()
    new = refresh_access_token(tokens["refresh_token"])
    save_tokens({"refresh_token": new["refresh_token"]})
    return new["access_token"]


def fetch_station_data(access_token: str) -> dict:
    log.info("Fetching station data...")
    r = http_request(
        "get",
        "https://api.netatmo.com/api/getstationsdata",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return r.json()


def fetch_homecoach_data(access_token: str) -> dict:
    log.info("Fetching Healthy Home Coach data...")
    r = http_request(
        "get",
        "https://api.netatmo.com/api/gethomecoachsdata",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return r.json()


def fetch_wind_speed() -> float:
    """Fetch current wind speed (km/h) from Open-Meteo. Returns 0 on failure."""
    if not LOCATION_LAT or not LOCATION_LON:
        log.warning("LOCATION_LAT/LON not set, skipping wind fetch")
        return 0.0
    try:
        r = http_request(
            "get",
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  LOCATION_LAT,
                "longitude": LOCATION_LON,
                "current":   "wind_speed_10m",
            },
        )
        wind = r.json()["current"]["wind_speed_10m"]
        log.info("Wind speed: %s km/h", wind)
        return float(wind)
    except Exception as e:
        log.warning("Failed to fetch wind speed: %s", _describe(e))
        return 0.0


def _eaqi(pm25: float | None, pm10: float | None,
          no2: float | None = None, o3: float | None = None) -> int:
    """European Air Quality Index, 1 (best) – 5 (worst)."""
    scores = []
    if pm25 is not None:
        if pm25 <= 10:    scores.append(1)
        elif pm25 <= 20:  scores.append(2)
        elif pm25 <= 25:  scores.append(3)
        elif pm25 <= 50:  scores.append(4)
        else:             scores.append(5)
    if pm10 is not None:
        if pm10 <= 20:    scores.append(1)
        elif pm10 <= 40:  scores.append(2)
        elif pm10 <= 50:  scores.append(3)
        elif pm10 <= 100: scores.append(4)
        else:             scores.append(5)
    if no2 is not None:
        if no2 <= 40:     scores.append(1)
        elif no2 <= 90:   scores.append(2)
        elif no2 <= 120:  scores.append(3)
        elif no2 <= 230:  scores.append(4)
        else:             scores.append(5)
    if o3 is not None:
        if o3 <= 50:      scores.append(1)
        elif o3 <= 100:   scores.append(2)
        elif o3 <= 130:   scores.append(3)
        elif o3 <= 240:   scores.append(4)
        else:             scores.append(5)
    return max(scores) if scores else 0


def _eaqi_label(score: int) -> str:
    return {1: "Velmi dobrá", 2: "Dobrá", 3: "Přijatelná", 4: "Špatná", 5: "Nezdravá"}.get(score, str(score))


def fetch_openaq() -> dict:
    """Fetch latest pollutant values from OPENAQ_LOCATION_ID via OpenAQ v3 /sensors. Returns {} on failure."""
    try:
        headers = {"Accept": "application/json", "X-API-Key": OPENAQ_API_KEY}
        r = http_request(
            "get",
            f"https://api.openaq.org/v3/locations/{OPENAQ_LOCATION_ID}/sensors",
            headers=headers,
        )

        pm25 = pm10 = no2 = o3 = None
        for sensor in r.json().get("results", []):
            param = sensor.get("parameter", {})
            raw   = param.get("name", "") if isinstance(param, dict) else str(param)
            name  = raw.lower().replace(".", "").replace(" ", "")
            latest = sensor.get("latest", {})
            value  = latest.get("value") if isinstance(latest, dict) else latest
            if value is None:
                continue
            if name == "pm25":   pm25 = round(float(value), 1)
            elif name == "pm10": pm10 = round(float(value), 1)
            elif name == "no2":  no2  = round(float(value), 1)
            elif name == "o3":   o3   = round(float(value), 1)

        out = {}
        if pm25 is not None: out["openaq_pm25"] = pm25
        if pm10 is not None: out["openaq_pm10"] = pm10
        if no2  is not None: out["openaq_no2"]  = no2
        if o3   is not None: out["openaq_o3"]   = o3
        score = _eaqi(pm25, pm10, no2, o3)
        if score:
            out["openaq_aqi"] = _eaqi_label(score)

        log.info("OpenAQ: location_id=%s pm25=%s pm10=%s no2=%s o3=%s aqi=%s",
                 OPENAQ_LOCATION_ID, pm25, pm10, no2, o3, out.get("openaq_aqi"))
        return out
    except Exception as e:
        log.warning("OpenAQ fetch failed: %s", _describe(e))
        return {}


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().strip().replace(" ", "_").replace("-", "_")


def feels_like(temp_c: float, humidity: int, wind_kmh: float = 0.0) -> float:
    """
    Composite feels-like matching Czech meteorological practice (ČHMÚ):
    - Wind chill (WMO/Environment Canada) for temp ≤ 14°C and wind > 4.8 km/h
    - Heat index (Rothfusz) for temp ≥ 27°C and humidity ≥ 40%
    - Otherwise raw temperature (no reliable formula for moderate conditions)
    """
    if temp_c <= 14 and wind_kmh > 4.8:
        wc = (13.12
              + 0.6215 * temp_c
              - 11.37 * wind_kmh ** 0.16
              + 0.3965 * temp_c * wind_kmh ** 0.16)
        if wc < temp_c:
            return round(wc, 1)

    if temp_c >= 27 and humidity >= 40:
        t, h = temp_c, humidity
        hi = (-8.78469475556
              + 1.61139411 * t
              + 2.33854883889 * h
              - 0.14611605 * t * h
              - 0.012308094 * t ** 2
              - 0.0164248277778 * h ** 2
              + 0.002211732 * t ** 2 * h
              + 0.00072546 * t * h ** 2
              - 0.000003582 * t ** 2 * h ** 2)
        return round(hi, 1)

    return temp_c


def _health_score(co2: int, humidity: int) -> int:
    """Derive 0-4 health score from CO2 and humidity, mirroring Netatmo Home Coach scale."""
    if co2 < 800:       co2_score = 0
    elif co2 < 1000:    co2_score = 1
    elif co2 < 1400:    co2_score = 2
    elif co2 < 2000:    co2_score = 3
    else:               co2_score = 4

    if 30 <= humidity <= 60:    hum_score = 0
    elif 25 <= humidity <= 70:  hum_score = 1
    elif 20 <= humidity <= 75:  hum_score = 2
    elif 15 <= humidity <= 80:  hum_score = 3
    else:                       hum_score = 4

    return max(co2_score, hum_score)


def health_label(score: int) -> str:
    """Czech one-word air quality label for the given 0–4 score."""
    return {0: "Zdravá", 1: "Dobrá", 2: "Přijatelná", 3: "Špatná", 4: "Nezdravá"}.get(score, str(score))


def parse_measurements(data: dict) -> dict:
    values = {}
    for device in data.get("body", {}).get("devices", []):
        dash = device.get("dashboard_data", {})

        if "Temperature" in dash:
            values["netatmo_indoor_temp"]     = round(dash["Temperature"], 1)
        if "Humidity" in dash:
            values["netatmo_indoor_humidity"] = dash["Humidity"]
        if "CO2" in dash:
            values["netatmo_indoor_co2"]      = dash["CO2"]
        if "Pressure" in dash:
            values["netatmo_pressure"]        = round(dash["Pressure"], 1)

        for module in device.get("modules", []):
            mtype = module.get("type", "")
            mdash = module.get("dashboard_data", {})

            if mtype == "NAModule1":  # Outdoor
                if "Temperature" in mdash:
                    values["netatmo_outdoor_temp"]     = round(mdash["Temperature"], 1)
                if "Humidity" in mdash:
                    values["netatmo_outdoor_humidity"] = mdash["Humidity"]

            elif mtype == "NAModule2":  # Wind
                if "WindStrength" in mdash:
                    values["netatmo_wind_speed"] = mdash["WindStrength"]
                if "GustStrength" in mdash:
                    values["netatmo_wind_gust"]  = mdash["GustStrength"]
                if "WindAngle" in mdash:
                    values["netatmo_wind_angle"] = mdash["WindAngle"]
                if "max_wind_str" in mdash:
                    values["netatmo_wind_max"]   = mdash["max_wind_str"]

            elif mtype == "NAModule3":  # Rain
                if "Rain" in mdash:
                    values["netatmo_rain_current"] = round(mdash["Rain"], 1)
                if "sum_rain_1" in mdash:
                    values["netatmo_rain_1h"]      = round(mdash["sum_rain_1"], 1)
                if "sum_rain_24" in mdash:
                    values["netatmo_rain_24h"]     = round(mdash["sum_rain_24"], 1)

    return values


def _coach_room(mac: str) -> str:
    return HOMECOACH_MAP.get(mac, mac.replace(":", "_")[-4:])


def parse_homecoach_measurements(data: dict) -> dict:
    """Returns measurements keyed by room slug (from HOMECOACH_MAP or MAC suffix)."""
    values = {}

    for device in data.get("body", {}).get("devices", []):
        dash = device.get("dashboard_data", {})
        mac  = device.get("_id", "00:00:00:00:00:00")

        room_raw = _coach_room(mac)
        slug     = _slugify(room_raw)
        log.info("Coach device: id=%s room=%s slug=%s", mac, room_raw, slug)

        if "Temperature" in dash:
            values[f"netatmo_{slug}_temp"]     = round(dash["Temperature"], 1)
        if "Humidity" in dash:
            values[f"netatmo_{slug}_humidity"] = dash["Humidity"]
        if "CO2" in dash:
            values[f"netatmo_{slug}_co2"]      = dash["CO2"]
        if "Noise" in dash:
            values[f"netatmo_{slug}_noise"]    = dash["Noise"]
        if "health_idx" in dash:
            values[f"netatmo_{slug}_health"]   = health_label(dash["health_idx"])

    return values


# ── data freshness ────────────────────────────────────────────────────────────

# Netatmo module type -> (name, keys that come from it, incl. derived ones)
STATION_MODULES = {
    "NAMain":    ("indoor",  ("netatmo_indoor_temp", "netatmo_indoor_humidity", "netatmo_indoor_co2",
                              "netatmo_pressure", "netatmo_indoor_health")),
    "NAModule1": ("outdoor", ("netatmo_outdoor_temp", "netatmo_outdoor_humidity",
                              "netatmo_outdoor_feels_like")),
    "NAModule2": ("wind",    ("netatmo_wind_speed", "netatmo_wind_gust", "netatmo_wind_angle",
                              "netatmo_wind_max")),
    "NAModule3": ("rain",    ("netatmo_rain_current", "netatmo_rain_1h", "netatmo_rain_24h")),
}
DEFAULT_STATION_MODULES = ("NAMain", "NAModule1")
COACH_FIELDS = ("temp", "humidity", "co2", "noise", "health")


def _utc_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%MZ")


def _local_dt(ts: float) -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DISPLAY_TZ)
    except Exception:
        tz = timezone.utc
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m. %H:%M")


def _check(name: str, node: dict | None, fallback_ts: float | None, now: float,
           missing_reason: str = "missing") -> dict:
    """Judge one module/device. `fallback_ts` = last timestamp we saw for it earlier."""
    dash = (node or {}).get("dashboard_data") or {}
    raw = dash.get("time_utc")
    seen_ts = raw if isinstance(raw, (int, float)) else None
    ts = seen_ts if seen_ts is not None else fallback_ts
    age = (now - ts) / 60 if ts is not None else None
    if node is None:
        reason = missing_reason
    elif not dash:
        reason = "no dashboard_data"
    elif node.get("reachable") is False:
        reason = "unreachable"
    elif seen_ts is None:
        reason = "no timestamp"
    elif age > STALE_AFTER_MIN:
        reason = "too old"
    else:
        reason = None
    return {"name": name, "stale": reason is not None, "reason": reason, "ts": ts, "age": age,
            "seen_ts": seen_ts}


def evaluate_station(data: dict, now: float, known: set, last_ts: dict,
                     missing_reason: str = "missing") -> tuple[dict, set, dict]:
    """Parse station data, dropping everything that is stale.

    Returns (values, stale_keys, infos keyed by module type). `known` (module types seen so
    far) and `last_ts` (module type -> last measurement time) are updated in place.
    """
    data = copy.deepcopy(data)
    infos = {}

    def visit(node):
        mtype = node.get("type")
        if mtype not in STATION_MODULES:
            return
        info = _check(STATION_MODULES[mtype][0], node, last_ts.get(mtype), now)
        infos[mtype] = info
        known.add(mtype)
        if info["seen_ts"] is not None:
            last_ts[mtype] = info["seen_ts"]
        if info["stale"]:
            node["dashboard_data"] = {}

    for device in data.get("body", {}).get("devices", []):
        device.setdefault("type", "NAMain")
        visit(device)
        for module in device.get("modules", []):
            visit(module)

    for mtype in known:
        if mtype not in infos:
            infos[mtype] = _check(STATION_MODULES[mtype][0], None, last_ts.get(mtype), now, missing_reason)

    stale_keys = {k for t, i in infos.items() if i["stale"] for k in STATION_MODULES[t][1]}
    return parse_measurements(data), stale_keys, infos


def evaluate_coaches(data: dict, now: float, known: set, last_ts: dict,
                     missing_reason: str = "missing") -> tuple[dict, set, dict]:
    """Like evaluate_station for Home Coach devices; `known`/`last_ts` are keyed by room slug."""
    data = copy.deepcopy(data)
    infos = {}
    for device in data.get("body", {}).get("devices", []):
        slug = _slugify(_coach_room(device.get("_id", "00:00:00:00:00:00")))
        info = _check(slug, device, last_ts.get(slug), now)
        infos[slug] = info
        known.add(slug)
        if info["seen_ts"] is not None:
            last_ts[slug] = info["seen_ts"]
        if info["stale"]:
            device["dashboard_data"] = {}

    for slug in known:
        if slug not in infos:
            infos[slug] = _check(slug, None, last_ts.get(slug), now, missing_reason)

    stale_keys = {f"netatmo_{slug}_{f}" for slug, i in infos.items() if i["stale"] for f in COACH_FIELDS}
    return parse_homecoach_measurements(data), stale_keys, infos


def describe_stale(source: str, infos: dict, n_values: int) -> str:
    parts = []
    for info in sorted((i for i in infos.values() if i["stale"]), key=lambda i: i["name"]):
        if info["ts"] is not None:
            text = f"{info['name']} last update {_utc_hm(info['ts'])} (age {info['age']:.0f} min)"
            if info["reason"] != "too old":
                text += f" [{info['reason']}]"
        else:
            text = f"{info['name']} no data [{info['reason']}]"
        parts.append(text)
    return f"{source} stale: {'; '.join(parts)}, skipping {n_values} values"


def describe_ages(infos: dict) -> str:
    return " ".join(
        f"{i['name']}={'?' if i['age'] is None else format(i['age'], '.0f')}min{'(stale)' if i['stale'] else ''}"
        for i in sorted(infos.values(), key=lambda i: i["name"])
    )


def apply_stale(values: dict, stale_keys: set) -> int:
    """Replace stale values by STALE_VALUE (or drop them in skip mode). Returns the number of keys."""
    for k in stale_keys:
        values.pop(k, None)
        if STALE_VALUE != STALE_SKIP:
            values[k] = STALE_VALUE
    return len(stale_keys)


# ── persistent state (/data) ──────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable state file: %s", type(e).__name__)
        return {}


def save_state(state: dict):
    try:
        _atomic_write(STATE_FILE, json.dumps(state))
    except OSError as e:
        log.warning("Cannot save state file: %s", type(e).__name__)


# ── alerts ────────────────────────────────────────────────────────────────────

def send_alert(event: str, message: str) -> bool:
    """Send via ntfy and/or webhook. True when delivered (or when no channel is configured)."""
    titles = {"stale": "Netatmo station: stale data", "reminder": "Netatmo station: still stale",
              "recovered": "Netatmo station: recovered"}
    title = titles[event]
    configured = delivered = False

    if NTFY_URL and NTFY_TOPIC:
        configured = True
        headers = {"Title": title,
                   "Priority": "default" if event == "recovered" else "high",
                   "Tags": "white_check_mark" if event == "recovered" else "warning"}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        try:
            http_request("post", f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}", data=message.encode("utf-8"),
                         headers=headers, log_url="ntfy")
            delivered = True
        except Exception as e:
            _fail("ntfy alert", e, logging.WARNING)

    if ALERT_WEBHOOK_URL:
        configured = True
        try:
            http_request("post", ALERT_WEBHOOK_URL, json={"event": event, "title": title, "message": message},
                         log_url="webhook")
            delivered = True
        except Exception as e:
            _fail("webhook alert", e, logging.WARNING)

    if CALLMEBOT_PHONE and CALLMEBOT_APIKEY:
        configured = True
        try:
            # CallMeBot only takes GET; apikey is in the query, so the URL is never logged
            http_request("get", "https://api.callmebot.com/whatsapp.php", log_url="callmebot",
                         params={"phone": CALLMEBOT_PHONE, "text": f"{title}\n{message}",
                                 "apikey": CALLMEBOT_APIKEY})
            delivered = True
        except Exception as e:
            _fail("callmebot alert", e, logging.WARNING)

    if not configured:
        log.warning("ALERT (%s, no alert channel configured): %s", event, message)
        return True
    return delivered


def process_alert(state: dict, stale: bool, detail: str, now: float):
    """Alert on ok->stale and stale->ok, plus a reminder every ALERT_REPEAT_HOURS while stale."""
    alert = state.setdefault("alert", {"status": "ok"})
    was_stale = alert.get("status") == "stale"
    if stale and not was_stale:
        event, message = "stale", f"Netatmo station is not sending current data: {detail}"
    elif not stale and was_stale:
        event, message = "recovered", "Netatmo station is sending current data again."
    elif (stale and ALERT_REPEAT_HOURS > 0
          and now - alert.get("last_alert", now) >= ALERT_REPEAT_HOURS * 3600):
        hours = (now - alert.get("since", now)) / 3600
        event, message = "reminder", f"Netatmo station has been stale for {hours:.1f} h: {detail}"
    else:
        return
    if not send_alert(event, message):
        return  # state unchanged -> next cycle tries again
    if stale:
        alert.update(status="stale", last_alert=now, since=alert.get("since", now) if was_stale else now)
    else:
        state["alert"] = {"status": "ok"}


# ── output ────────────────────────────────────────────────────────────────────

def push_to_zo(values: dict) -> bool:
    if not values:
        log.warning("No values to push")
        return False
    params = {"import_key": ZO_IMPORT_KEY, **values}
    log.info("Pushing %d values to Zivy obraz...", len(values))
    r = http_request("get", "https://in.zivyobraz.eu/", params=params, timeout=PUSH_TIMEOUT)
    log.info("OK: %s", r.text.strip())
    return True


def write_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        log.warning("Cannot write heartbeat: %s", type(e).__name__)


# ── main loop ─────────────────────────────────────────────────────────────────

def _down_too_long(state: dict, source: str, now: float) -> bool:
    """True when the API has been failing for longer than STALE_AFTER_MIN."""
    return now - state.setdefault("last_ok", {}).setdefault(source, now) > STALE_AFTER_MIN * 60


def _iteration() -> bool:
    now = time.time()
    state = load_state()
    last_ts = state.setdefault("last_ts", {})
    known_modules = set(state.get("known_modules", DEFAULT_STATION_MODULES))
    known_coaches = set(state.get("known_coaches", ())) | {_slugify(v) for v in HOMECOACH_MAP.values()}

    token = None
    try:
        token = get_access_token()
    except Exception as e:
        _fail("Netatmo token refresh", e)

    def fetch(what, fn):
        if token is None or _stop.is_set():
            return None
        try:
            return fn(token)
        except Exception as e:
            _fail(what, e)
            return None

    station_data = fetch("Netatmo station fetch", fetch_station_data)
    coach_data   = fetch("Netatmo homecoach fetch", fetch_homecoach_data)
    if _stop.is_set():
        return False

    values = {}
    stale_by_source = {}

    # station
    st_last = {k.split(":", 1)[1]: v for k, v in last_ts.items() if k.startswith("station:")}
    station_infos = None
    if station_data is not None:
        state.setdefault("last_ok", {})["station"] = now
        v, k, station_infos = evaluate_station(station_data, now, known_modules, st_last)
    elif _down_too_long(state, "station", now):
        v, k, station_infos = evaluate_station({}, now, known_modules, st_last, "api unavailable")
    if station_infos is not None:
        values.update(v)
        stale_by_source["station"] = k
        last_ts.update({f"station:{t}": ts for t, ts in st_last.items()})

    # home coaches
    co_last = {k.split(":", 1)[1]: v for k, v in last_ts.items() if k.startswith("coach:")}
    coach_infos = None
    if coach_data is not None:
        state.setdefault("last_ok", {})["coach"] = now
        v, k, coach_infos = evaluate_coaches(coach_data, now, known_coaches, co_last)
    elif _down_too_long(state, "coach", now):
        v, k, coach_infos = evaluate_coaches({}, now, known_coaches, co_last, "api unavailable")
    if coach_infos is not None:
        values.update(v)
        stale_by_source["coach"] = k
        last_ts.update({f"coach:{s}": ts for s, ts in co_last.items()})

    state["known_modules"] = sorted(known_modules)
    state["known_coaches"] = sorted(known_coaches)

    ages = {}
    if station_infos:
        ages.update({f"station.{i['name']}": i for i in station_infos.values()})
    if coach_infos:
        ages.update({f"coach.{i['name']}": i for i in coach_infos.values()})
    log.info("Measurements: %s", values)
    if ages:
        log.info("Measurement ages: %s", describe_ages(ages))
    for source, infos in (("station", station_infos), ("coach", coach_infos)):
        if infos and any(i["stale"] for i in infos.values()):
            keys = sorted(stale_by_source[source])
            log.warning(describe_stale(source, infos, len(keys)))
            log.warning("%s stale keys: %s", source, ", ".join(keys))

    # air quality from OpenAQ / ČHMÚ (failures are handled inside)
    values.update(fetch_openaq())

    # derived station values - only from fresh data
    outdoor_temp = values.get("netatmo_outdoor_temp")
    if isinstance(outdoor_temp, (int, float)):
        wind_kmh = float(values.get("netatmo_wind_speed", 0) or fetch_wind_speed())
        outdoor_humidity = values.get("netatmo_outdoor_humidity", 50)
        if isinstance(outdoor_humidity, (int, float)):
            values["netatmo_outdoor_feels_like"] = feels_like(outdoor_temp, outdoor_humidity, wind_kmh)
    co2 = values.get("netatmo_indoor_co2")
    hum = values.get("netatmo_indoor_humidity")
    if isinstance(co2, int) and isinstance(hum, int):
        values["netatmo_indoor_health"] = health_label(_health_score(co2, hum))

    # station status for the display template + alerting
    if station_infos is not None:
        station_stale = any(i["stale"] for i in station_infos.values())
        values["netatmo_station_stale"] = int(station_stale)
        tss = [i["ts"] for i in station_infos.values() if i["ts"] is not None]
        if tss:
            values["netatmo_station_last_update"] = _local_dt(min(tss))
        try:
            process_alert(state, station_stale,
                          describe_stale("station", station_infos, len(stale_by_source["station"])), now)
        except Exception as e:
            _fail("alert processing", e)

    blanked = apply_stale(values, set().union(*stale_by_source.values()))
    save_state(state)
    if _stop.is_set():
        return False
    if blanked:
        log.info("%d stale values %s", blanked,
                 "omitted" if STALE_VALUE == STALE_SKIP else f"replaced by {STALE_VALUE!r}")
    try:
        if not push_to_zo(values):
            return False
    except Exception as e:
        _fail("Zivy obraz push", e)
        return False
    write_heartbeat()
    return True


def run_once() -> bool:
    """One full cycle. Never raises; a failure is one ERROR line (traceback at DEBUG)."""
    try:
        return _iteration()
    except Exception as e:
        _fail("Iteration", e)
        return False


def _on_signal(signum, frame):
    _stop.set()  # no logging here: it could deadlock on the logging lock


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if "--once" in argv:
        return 0 if run_once() else 1
    log.info("Starting, interval %d s, stale after %d min", INTERVAL_SEC, STALE_AFTER_MIN)
    while not _stop.is_set():
        run_once()
        if _stop.wait(INTERVAL_SEC):
            break
    log.info("Stopping on signal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
