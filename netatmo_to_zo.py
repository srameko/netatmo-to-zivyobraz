#!/usr/bin/env python3
"""
Netatmo -> Zivy obraz
Fetches data from the Netatmo API and OpenAQ, then pushes everything
to Zivy obraz as custom values. Intended to run every 15 minutes.
"""

import copy
import html
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from email.header import Header
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


def _int_setting(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Integer setting from a Swarm secret or an env var, clamped to lo..hi.

    A value that is not a number (a typo, a path left in the wrong variable) falls back to the default with
    a warning: a setting of an optional feature must never keep the whole container from starting.
    """
    raw = _secret(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a whole number, using %d", name, raw, default)
        return default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


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

# Netatmo's public status page (JSON behind health.netatmo.com). "" disables the lookup.
STATUS_API      = os.environ.get("NETATMO_STATUS_API", "https://health.netatmo.com/api").rstrip("/")
STATUS_SERVICES = re.compile(r"weather|homecoach|3rd party|cloud", re.I)

# optional BUSY Bar output (empty URL = off), e.g. http://192.168.5.196/api (LAN, password)
# or https://api.busy.app/busybar (cloud, API token)
BUSYBAR_URL      = _secret("BUSYBAR_URL", "").rstrip("/")
BUSYBAR_TOKEN    = _secret("BUSYBAR_TOKEN", "")
BUSYBAR_PRIORITY = _int_setting("BUSYBAR_PRIORITY", 50, 1, 100)   # 10 built-in apps, 90 BUSY/CUSTOM session
BUSYBAR_ROTATE_SEC = _int_setting("BUSYBAR_ROTATE_SEC", 8, 1)     # seconds each screen is shown in a glance
BUSYBAR_GLANCE_MIN = _int_setting("BUSYBAR_GLANCE_MIN", 16, 1)    # minutes between glances
BUSYBAR_APP      = "netatmo"

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


def _cause_chain(exc: BaseException) -> str:
    """Class names of the exceptions wrapped inside `exc`, e.g. ProtocolError>RemoteDisconnected (no messages)."""
    names = []
    cur: BaseException | None = exc
    for _ in range(4):
        inner = getattr(cur, "reason", None)
        if not isinstance(inner, BaseException):
            inner = next((a for a in getattr(cur, "args", ()) if isinstance(a, BaseException)), None)
        if inner is None:
            break
        names.append(type(inner).__name__)
        cur = inner
    return ">".join(names)


def _describe(exc: Exception) -> str:
    """One-line, secret-free description of a failure (never str(exc): it may hold the full URL)."""
    parts = [type(exc).__name__]
    cause = _cause_chain(exc)
    if cause:
        parts.append(f"cause={cause}")
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


def http_request(method: str, url: str, *, log_url: str | None = None, retry: bool = True, **kwargs):
    """requests.<method> with a default timeout and retries (5xx, 429, network errors).

    4xx other than 429 is raised at once. The raised exception carries `log_url`
    (the URL without query string) for logging.
    """
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    delays = RETRY_DELAYS if retry else ()
    for attempt in range(len(delays) + 1):
        try:
            r = getattr(requests, method)(url, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            e.log_url = log_url or _safe_url(url)
            if not _retryable(e) or attempt == len(delays):
                raise
            delay = delays[attempt]
            log.warning("Retry %d/%d in %ds after %s", attempt + 1, len(delays), delay, _describe(e))
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
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(DISPLAY_TZ)
    except Exception:
        return timezone.utc


def _local_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, _tz()).strftime("%d.%m. %H:%M")


def _local_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, _tz()).strftime("%H:%M")


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


_CS_NAMES = {"indoor": "uvnitř", "outdoor": "venku", "wind": "vítr", "rain": "srážky"}
_CS_REASONS = {"unreachable": "modul nedostupný", "missing": "modul chybí", "no dashboard_data": "bez dat",
               "no timestamp": "bez časové značky", "api unavailable": "Netatmo API nedostupné"}


def describe_stale_cs(infos: dict, n_values: int) -> str:
    """Czech, local-time description for alerts."""
    parts = []
    for info in sorted((i for i in infos.values() if i["stale"]), key=lambda i: i["name"]):
        name = _CS_NAMES.get(info["name"], info["name"]).capitalize()
        reason = _CS_REASONS.get(info["reason"])
        if info["ts"] is not None:
            text = f"{name}: poslední měření v {_local_hm(info['ts'])} (před {info['age']:.0f} min)"
        else:
            text = f"{name}: žádná data"
        parts.append(text + (f", {reason}" if reason else ""))
    return "; ".join(parts) + f". Dotčených hodnot: {n_values}."


def describe_raw_station(data: dict, now: float) -> str:
    """Compact, secret-free dump of what the API says about each station module (for diagnosing outages)."""
    def age(ts):
        return "?" if not isinstance(ts, (int, float)) else f"{(now - ts) / 60:.0f}min"

    parts = []

    def node(n):
        dash = n.get("dashboard_data")
        parts.append(
            f"{n.get('type', '?')}[reachable={n.get('reachable')} wifi={n.get('wifi_status')} "
            f"rf={n.get('rf_status')} battery={n.get('battery_percent')} last_seen={age(n.get('last_seen'))} "
            f"last_store={age(n.get('last_status_store'))} "
            f"dashboard={age(dash.get('time_utc')) if dash else 'none'}]")

    for device in data.get("body", {}).get("devices", []):
        node(device)
        for module in device.get("modules", []):
            node(module)
    return " ".join(parts) if parts else "no devices in response"


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


def measurement_expiry(*infos_by_source: dict | None) -> dict[str, float]:
    """Value key -> time (epoch s) from which its reading counts as stale.

    Only readings that were fresh when fetched get a deadline: measurement time + STALE_AFTER_MIN.
    Readings that were already stale are replaced by STALE_VALUE at fetch time and need none.
    """
    limit = STALE_AFTER_MIN * 60
    expiry: dict[str, float] = {}
    for infos in infos_by_source:
        for key, info in (infos or {}).items():
            if info["stale"] or info["ts"] is None:
                continue
            keys = STATION_MODULES[key][1] if key in STATION_MODULES else \
                tuple(f"netatmo_{info['name']}_{f}" for f in COACH_FIELDS)
            for k in keys:
                expiry[k] = info["ts"] + limit
    return expiry


def fetch_netatmo_incident() -> str | None:
    """Active Netatmo incident touching weather/cloud/API services, from their status page. Never raises."""
    if not STATUS_API:
        return None
    try:
        def get(path, **params):
            return http_request("get", f"{STATUS_API}/{path}", params=params, timeout=(3, 8), retry=False).json()

        posts = get("posts", is_featured="true", limit=50)["posts"]
        if not posts:
            return None
        names = {s["id"]: s["name"] for s in get("services")["services"]}
        statuses = {e["id"]: e["name"] for e in get("post_enums")["post_enums"]
                    if e.get("post_enum_type") == "status"}
        for post in posts:
            update = post.get("latest_update") or {}
            status = statuses.get(update.get("status_id"), "?")
            if post.get("post_type") != "incident" or status == "resolved":
                continue
            hit = sorted({names[i["service_id"]] for i in update.get("impacts", [])
                          if names.get(i["service_id"]) and STATUS_SERVICES.search(names[i["service_id"]])})
            if hit:
                text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", update.get("message", ""))).split())
                return f"{post.get('title', 'incident')} [{status}; {', '.join(hit)}] {text[:220]}".strip()
    except Exception as e:
        log.debug("Netatmo status page lookup failed: %s", _describe(e))
    return None


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
    titles = {"stale": "Netatmo: zastaralá data", "reminder": "Netatmo: data stále zastaralá",
              "recovered": "Netatmo: stanice obnovena"}
    title = titles[event]
    configured = delivered = False

    if NTFY_URL and NTFY_TOPIC:
        configured = True
        headers = {"Title": Header(title, "utf-8").encode(),  # RFC 2047, HTTP headers are not UTF-8
                   "Priority": "default" if event == "recovered" else "high",
                   "Tags": "white_check_mark" if event == "recovered" else "warning"}
        if NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
        try:
            http_request("post", f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}", data=message.encode("utf-8"),
                         headers=headers, log_url="ntfy")
            delivered = True
            log.info("Alert sent via ntfy: %s", event)
        except Exception as e:
            _fail("ntfy alert", e, logging.WARNING)

    if ALERT_WEBHOOK_URL:
        configured = True
        try:
            http_request("post", ALERT_WEBHOOK_URL, json={"event": event, "title": title, "message": message},
                         log_url="webhook")
            delivered = True
            log.info("Alert sent via webhook: %s", event)
        except Exception as e:
            _fail("webhook alert", e, logging.WARNING)

    if CALLMEBOT_PHONE and CALLMEBOT_APIKEY:
        configured = True
        try:
            # CallMeBot only takes GET; apikey is in the query, so the URL is never logged
            r = http_request("get", "https://api.callmebot.com/whatsapp.php", log_url="callmebot",
                             params={"phone": CALLMEBOT_PHONE, "text": f"{title}\n{message}",
                                     "apikey": CALLMEBOT_APIKEY})
            # CallMeBot answers HTTP 200 even when it refuses the message
            if "paused" in r.text.lower():
                log.error("CallMeBot alert not delivered: account is Paused (re-activate the API key)")
            else:
                log.debug("CallMeBot response: %s", _redact(" ".join(r.text.split())[:200]))
                delivered = True
                log.info("Alert sent via callmebot: %s", event)
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
        event, message = "stale", f"Stanice Netatmo neposílá aktuální data. {detail}"
    elif not stale and was_stale:
        minutes = (now - alert.get("since", now)) / 60
        event, message = "recovered", ("Stanice Netatmo opět posílá aktuální data "
                                       f"(výpadek trval {int(minutes // 60)} h {int(minutes % 60)} min).")
    elif (stale and ALERT_REPEAT_HOURS > 0
          and now - alert.get("last_alert", now) >= ALERT_REPEAT_HOURS * 3600):
        hours = (now - alert.get("since", now)) / 3600
        event, message = "reminder", f"Stanice Netatmo neposílá aktuální data už {hours:.1f} h. {detail}"
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


def _ascii(text: str) -> str:
    """BUSY Bar fonts are bitmap ASCII: strip diacritics and anything non-printable."""
    folded = unicodedata.normalize("NFKD", text)
    return "".join(c for c in folded if " " <= c <= "~")


def _bar_text(value, fmt: str) -> str | None:
    """Format one value for the Bar, or None when there is nothing sensible to show."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return fmt.format(value)
    text = _ascii(str(value)).strip()
    return text or None   # stale marker ("--") is shown as is; empty -> no element


VENT_CO2 = 1000   # ppm: from here on the room needs airing

# 5x10 thermometer; "{color}" is filled in per reading
_THERMOMETER_XPM = (
    "! XPM2\n5 10 2 1\n. c none\no c {color}\n"
    "..o..\n.ooo.\n.ooo.\n.ooo.\n.ooo.\n.ooo.\nooooo\nooooo\nooooo\n.ooo.\n"
)

# 9x9 snowflake for temperatures below zero
_FROST_XPM = (
    "! XPM2\n9 9 2 1\n. c none\no c #7DD3FC\n"
    "....o....\n.o..o..o.\n..o.o.o..\n...ooo...\nooooooooo\n...ooo...\n..o.o.o..\n.o..o..o.\n....o....\n"
)

# 9x10 rain cloud with drops
_RAIN_XPM = (
    "! XPM2\n9 10 3 1\n. c none\nc c #9CA3AF\nb c #60A5FA\n"
    "..ccc....\n.ccccccc.\nccccccccc\nccccccccc\n.ccccccc.\n"
    ".........\n.b..b..b.\n..b..b..b\n.b..b..b.\n..b..b..b\n"
)

# 11x12 window with the sash swung open and air streaming through; "{color}" is the frame (per severity)
_WINDOW_XPM = (
    "! XPM2\n11 12 6 1\n. c none\no c {color}\ng c #3B82F6\nh c #BFDBFE\nk c #1E3A8A\nw c #E5E5E5\n"
    "ooooooooooo\nohhgokkkkko\nohggokkkkko\nogggokkkkko\nogggokwwwko\nogggokkkkko\n"
    "ogggokkkkko\nogggokkwwko\nogggokkkkko\nogggokkkkko\nogggokkkkko\nooooooooooo\n"
)

_ROOM_NAMES = {"indoor": "uvnitr"}   # the station's own module has no HOMECOACH_MAP slug
_Z_BG, _Z_FG = 1000, 1001            # our frame covers whatever another app left on the display
_TEMP_X = 14                         # temperature starts here: a few pixels clear of the icon


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bold_width(text: str) -> int:
    """Pixel width of `text` in the Bar's bold font (digits 7 px, '-' and '.' 4 px)."""
    return sum(7 if c.isdigit() else 4 for c in text)


def _hum_width(text: str) -> int:
    """Pixel width of `text` in the Bar's "normal" font (digits 6 px, "%" 8 px)."""
    return sum(6 if c.isdigit() else 8 for c in text)


def _temp_color(value) -> str:
    if not _is_number(value):
        return "#808080"
    return "#60A5FA" if value < 5 else "#F87171" if value >= 27 else "#E5E5E5"


def _co2_color(value) -> str:
    if not _is_number(value):
        return "#808080FF"
    return "#4ADE80FF" if value < 800 else "#FACC15FF" if value < 1200 else "#F87171FF"


def _is_raining(values: dict) -> bool:
    rain = values.get("netatmo_rain_current")
    return _is_number(rain) and rain > 0


def _alert_state(values: dict) -> tuple[bool, bool]:
    """(it rains, some room needs airing): what makes a glance worth showing at once."""
    return _is_raining(values), bool(_rooms_to_air(values))


def _rooms_to_air(values: dict) -> list[tuple[str, int]]:
    """Rooms whose CO2 is at or above VENT_CO2, worst first. Stale ("--") readings are ignored."""
    rooms = []
    for key, value in values.items():
        m = re.fullmatch(r"netatmo_(.+)_co2", key)
        if m and _is_number(value) and value >= VENT_CO2:
            rooms.append((_ascii(_ROOM_NAMES.get(m.group(1), m.group(1))), int(value)))
    return sorted(rooms, key=lambda r: -r[1])


def build_busybar_frames(values: dict) -> list[dict]:
    """The screens the front display (72x16) takes turns showing, as ready draw requests.

    1. weather: icon (rain cloud while it rains, else a thermometer), the outdoor temperature, its unit and
       the outdoor humidity - only with a temperature reading;
    2. airing: a window icon, "Vetrejte!" and the rooms at or above VENT_CO2 (worst first, scrolls when long) -
       only while a room needs it.

    Both frames use the same element ids, so drawing one replaces the other, and both start with a black
    background that hides what another app left on the display. Elements carry a relative `timeout` (not an
    absolute time, so the Bar's clock does not matter) and vanish on their own if the container stops. An
    empty list means there is nothing to show.
    """
    def el(eid: str, kind: str, x: int, y: int, align: str = "top_left", z: int = _Z_FG, **fields) -> dict:
        return {"id": eid, "type": kind, "x": x, "y": y, "align": align, "z_index": z,
                "display": "front", **fields}

    def frame(*elements: dict) -> dict:
        bg = el("bg", "rectangle", 0, 0, z=_Z_BG, width=72, height=16, fill="solid",
                fill_colors=["#000000FF"], border_width=0)
        return {"application_name": BUSYBAR_APP, "priority": BUSYBAR_PRIORITY, "elements": [bg, *elements]}

    frames = []
    temp_val = values.get("netatmo_outdoor_temp")
    temp = _bar_text(temp_val, "{:.1f}")
    if temp == "-0.0":
        temp = "0.0"
    if temp:
        if _is_raining(values):
            pic = el("pic", "xpmbitmap", 1, 3, data=_RAIN_XPM)
        elif _is_number(temp_val) and round(temp_val, 1) < 0:   # by the shown reading: "0.0" is no frost
            pic = el("pic", "xpmbitmap", 1, 4, data=_FROST_XPM)       # frost: a snowflake instead of the thermometer
        else:
            pic = el("pic", "xpmbitmap", 3, 3, data=_THERMOMETER_XPM.format(color=_temp_color(temp_val)))
        hum = _bar_text(values.get("netatmo_outdoor_humidity"), "{:.0f}%")
        room_until = 72 - _hum_width(hum) - 2 if hum else 72      # the unit must not touch the humidity
        unit_x = _TEMP_X + _bold_width(temp) + 1
        unit = (el("s1", "text", unit_x, 3, text="C", font="small", color="#A0A0A0FF")
                if _is_number(temp_val) and unit_x + 5 <= room_until else
                el("s1", "text", _TEMP_X, 3, text=" ", font="small"))
        hum_el = (el("s2", "text", 72, 8, "mid_right", text=hum, font="normal", color="#60A5FAFF") if hum else
                  el("s2", "text", _TEMP_X, 3, text=" ", font="small"))
        frames.append(frame(pic, el("big", "text", _TEMP_X, 3, text=temp, font="bold", color="#FFFFFFFF"), unit, hum_el))

    rooms = _rooms_to_air(values)
    if rooms:
        color = _co2_color(rooms[0][1])
        message = ", ".join(f"{name} {ppm}" for name, ppm in rooms)
        frames.append(frame(
            el("pic", "xpmbitmap", 1, 2, data=_WINDOW_XPM.format(color=color[:7])),
            el("big", "text", 14, -2, text="Vetrejte!", font="bold", color=color),
            el("s1", "text", 14, 8, text=message, font="small", color="#E5E5E5FF", width=58,
               scroll_rate=1500, scroll_start_delay=1500, scroll_repeat_delay=2500),
            el("s2", "text", 14, 8, text=" ", font="small"),
        ))
    # a screen is replaced or cleared after BUSYBAR_ROTATE_SEC; the timeout only covers a container that dies midway
    ttl = 2 * BUSYBAR_ROTATE_SEC + 5
    for f in frames:
        for e in f["elements"]:
            e["timeout"] = ttl
    return frames


# What the Bar last said, so that a long outage or a busy Bar is logged once, not every few seconds.
_bar_state = "unknown"


def _bar_report(state: str, level: int, message: str) -> None:
    global _bar_state
    if state != _bar_state:
        log.log(level, "BUSY Bar: %s", message)
    _bar_state = state


def _bar_headers() -> dict:
    # cloud API takes a Bearer token, the local (LAN) API an X-API-Token header
    auth = ({"Authorization": f"Bearer {BUSYBAR_TOKEN}"} if urlsplit(BUSYBAR_URL).hostname == "api.busy.app"
            else {"X-API-Token": BUSYBAR_TOKEN})
    return {
        **auth,
        "User-Agent": "netatmo-to-zivyobraz",   # Cloudflare blocks python-requests' default
        # The Bar's small HTTP server never answers a keep-alive request (requests' default): the call hangs until
        # the read timeout. With `Connection: close` it answers at once (measured: 0.0 s against 8 s timeouts).
        "Connection": "close",
    }


def push_to_busybar(payload: dict) -> bool:
    """Draw one frame. Never raises; a failure never affects the Zivy obraz push."""
    if not BUSYBAR_URL:
        return False
    try:
        http_request("post", f"{BUSYBAR_URL}/display/draw", json=payload, headers=_bar_headers(),
                     timeout=HTTP_TIMEOUT, retry=False)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 409:
            _bar_report("busy", logging.INFO, "a higher-priority app is on screen, waiting")
        else:
            _bar_report("error", logging.WARNING, f"push failed: {_describe(e)}")
        return False
    except Exception as e:
        _bar_report("error", logging.WARNING, f"push failed: {_describe(e)}")
        return False
    _bar_report("ok", logging.INFO, "drawing")
    return True


def clear_busybar() -> None:
    """Take our screens off the display so the app underneath (e.g. the clock) is visible again."""
    if not BUSYBAR_URL:
        return
    try:
        http_request("delete", f"{BUSYBAR_URL}/display/draw", params={"application_name": BUSYBAR_APP},
                     headers=_bar_headers(), timeout=HTTP_TIMEOUT, retry=False)
    except Exception as e:          # nothing of ours on screen (400) or the Bar is away: the timeout clears it anyway
        log.debug("BUSY Bar clear: %s", _describe(e))


class _BusyBarRotator:
    """Shows the Bar's screens now and then, and gives the display back in between.

    A glance shows each screen for BUSYBAR_ROTATE_SEC and then clears our elements, so the app that was
    running (the clock) is untouched except for those few seconds. A glance starts when the first readings
    arrive, every BUSYBAR_GLANCE_MIN minutes after that, and at once when it starts to rain or a room starts
    to need airing. A busy or unreachable Bar is tried again a minute later.
    """

    def __init__(self, background: bool = True):
        self.background = background     # False (--once): one glance right in update(), no thread outliving the run
        self._lock = threading.Lock()
        self._values: dict | None = None
        self._expiry: dict[str, float] = {}
        self._stamp = 0.0
        self._thread: threading.Thread | None = None
        self._next_glance = 0.0          # epoch seconds; 0 = as soon as there is something to show
        self._reason = "first readings"  # why the next glance starts (for the log)
        self._failures = 0               # glances in a row that could not be drawn
        self._alerts = (False, False)    # (raining, a room needs airing) as of the last readings
        self._queue: list[dict] = []     # screens of the glance in progress, still to be drawn
        self._showing = False

    def update(self, values: dict, expiry: dict[str, float] | None = None, now: float | None = None) -> None:
        """New readings. `expiry` maps value keys to the time their reading turns stale (see measurement_expiry)."""
        if not BUSYBAR_URL:
            return
        with self._lock:
            self._values = dict(values)
            self._expiry = dict(expiry or {})
            self._stamp = time.time() if now is None else now
            alerts = _alert_state(values)
            started = [name for name, new, old in zip(("rain started", "airing needed"), alerts, self._alerts)
                       if new and not old]
            if started:                  # it just started to rain / a room just started to need airing
                self._next_glance = 0.0
                self._reason = " + ".join(started)
            self._alerts = alerts
            if self.background and self._thread is None:
                self._thread = threading.Thread(target=self._run, name="busybar", daemon=True)
                self._thread.start()
        if not self.background:
            self._glance_blocking(time.time())

    def _readings(self, now: float) -> dict | None:
        """The latest values with aged readings shown as stale; None when there is no fresh data."""
        with self._lock:
            values, stamp, expiry = self._values, self._stamp, self._expiry
        if values is None or now - stamp > STALE_AFTER_MIN * 60:
            return None
        aged = {k for k, deadline in expiry.items() if now >= deadline and k in values}
        if aged:
            values = dict(values)
            apply_stale(values, aged)
        return values

    def tick(self, now: float) -> bool:
        """One step: idle (no request), start a glance, show its next screen or give the display back."""
        if self._showing:
            if self._queue:
                if push_to_busybar(self._queue.pop(0)):
                    return True
                self._queue, self._showing = [], False          # busy or away: stop and back off
                clear_busybar()
                self._retry_later(now)
                return False
            self._showing = False
            clear_busybar()
            return True
        if now < self._next_glance:
            return False
        values = self._readings(now)
        frames = build_busybar_frames(values) if values is not None else []
        self._next_glance = now + BUSYBAR_GLANCE_MIN * 60
        reason, self._reason = self._reason, "scheduled"
        if not frames:
            return False
        log.info("BUSY Bar: glance (%s), %d screen(s)", reason, len(frames))
        if not push_to_busybar(frames[0]):
            self._retry_later(now)
            return False
        self._failures = 0
        self._queue, self._showing = frames[1:], True
        return True

    def _retry_later(self, now: float) -> None:
        """A glance that could not be drawn is tried again after 1, 2, 4, ... minutes, never later than a
        regular glance. A Bar that draws but never answers must not be redrawn every minute."""
        self._failures += 1
        delay = min(60 * 2 ** (self._failures - 1), BUSYBAR_GLANCE_MIN * 60)
        self._next_glance = now + delay
        self._reason = "retry"
        log.info("BUSY Bar: glance not drawn (%d in a row), next try in %d min", self._failures, round(delay / 60))

    def _glance_blocking(self, now: float) -> None:
        """--once: the whole glance right here (no thread that the exiting process would cut short)."""
        values = self._readings(now)
        frames = build_busybar_frames(values) if values is not None else []
        for frame in frames:
            if not push_to_busybar(frame):
                return
            _stop.wait(BUSYBAR_ROTATE_SEC)
        if frames:
            clear_busybar()

    def _run(self) -> None:
        while not _stop.is_set():
            try:
                self.tick(time.time())
            except Exception as e:
                _fail("BUSY Bar rotation", e, logging.WARNING)
            _stop.wait(BUSYBAR_ROTATE_SEC)


busybar = _BusyBarRotator()


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
        log.info("Station raw: %s", describe_raw_station(station_data, now))
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
        bad = sorted(f"{i['name']}({i['reason']})" for i in station_infos.values() if i["stale"])
        oldest = max((i["age"] for i in station_infos.values() if i["age"] is not None), default=None)
        log.info("Station: %s, oldest data age %s min%s", "STALE" if station_stale else "OK",
                 "?" if oldest is None else f"{oldest:.0f}", f", problems: {', '.join(bad)}" if bad else "")
        tss = [i["ts"] for i in station_infos.values() if i["ts"] is not None]
        if tss:
            values["netatmo_station_last_update"] = _local_dt(min(tss))
        detail = describe_stale_cs(station_infos, len(stale_by_source["station"]))
        if station_stale:
            incident = fetch_netatmo_incident()
            if incident:
                log.warning("Netatmo status page reports: %s", incident)
                detail += f" Netatmo na stavové stránce hlásí problém: {incident}"
            else:
                log.info("Netatmo status page: no active weather/cloud incident")
        try:
            process_alert(state, station_stale, detail, now)
        except Exception as e:
            _fail("alert processing", e)

    blanked = apply_stale(values, set().union(*stale_by_source.values()))
    save_state(state)
    if _stop.is_set():
        return False
    if blanked:
        log.info("%d stale values %s", blanked,
                 "omitted" if STALE_VALUE == STALE_SKIP else f"replaced by {STALE_VALUE!r}")
    # the Bar is a separate destination: it gets the fresh values even when the Zivy obraz push fails
    busybar.update(values, measurement_expiry(station_infos, coach_infos))
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


def check_busybar_config() -> None:
    """Say so at startup when the Bar output is on but cannot work (it would only fail quietly every cycle)."""
    if BUSYBAR_URL and not BUSYBAR_TOKEN:
        log.warning("BUSY Bar: BUSYBAR_URL is set but BUSYBAR_TOKEN is empty, every draw will be refused")
    elif BUSYBAR_URL:
        log.info("BUSY Bar output on: %s, priority %d, a glance every %d min (%d s per screen) and when it starts "
                 "to rain or a room needs airing", _safe_url(BUSYBAR_URL), BUSYBAR_PRIORITY,
                 BUSYBAR_GLANCE_MIN, BUSYBAR_ROTATE_SEC)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    check_busybar_config()
    if "--once" in argv:
        busybar.background = False      # a daemon thread would die with the process before it drew anything
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
