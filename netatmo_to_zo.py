#!/usr/bin/env python3
"""
Netatmo -> Zivy obraz
Fetches data from the Netatmo API, asks an LLM for clothing and ventilation
advice, and pushes everything to Zivy obraz as custom values.
Intended to run every 15 minutes (Docker CMD loop).
"""

import os
import json
import logging
import requests
import unicodedata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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

OLLAMA_URL      = _secret("OLLAMA_URL", "http://rpi.home:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")
LOCATION_LAT    = float(os.environ.get("LOCATION_LAT", "0"))
LOCATION_LON    = float(os.environ.get("LOCATION_LON", "0"))
OPENAQ_STATION  = os.environ.get("OPENAQ_STATION", "Brno-Svatoplukova")

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


def load_tokens() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return {"refresh_token": NETATMO_REFRESH_TOKEN}


def save_tokens(tokens: dict):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f)


def refresh_access_token(refresh_token: str) -> dict:
    log.info("Refreshing access token...")
    r = requests.post(
        "https://api.netatmo.com/oauth2/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     NETATMO_CLIENT_ID,
            "client_secret": NETATMO_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_access_token() -> str:
    tokens = load_tokens()
    new = refresh_access_token(tokens["refresh_token"])
    save_tokens({"refresh_token": new["refresh_token"]})
    return new["access_token"]


def fetch_station_data(access_token: str) -> dict:
    log.info("Fetching station data...")
    r = requests.get(
        "https://api.netatmo.com/api/getstationsdata",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_homecoach_data(access_token: str) -> dict:
    log.info("Fetching Healthy Home Coach data...")
    r = requests.get(
        "https://api.netatmo.com/api/gethomecoachsdata",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_wind_speed() -> float:
    """Fetch current wind speed (km/h) from Open-Meteo. Returns 0 on failure."""
    if not LOCATION_LAT or not LOCATION_LON:
        log.warning("LOCATION_LAT/LON not set, skipping wind fetch")
        return 0.0
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  LOCATION_LAT,
                "longitude": LOCATION_LON,
                "current":   "wind_speed_10m",
            },
            timeout=10,
        )
        r.raise_for_status()
        wind = r.json()["current"]["wind_speed_10m"]
        log.info("Wind speed: %s km/h", wind)
        return float(wind)
    except Exception as e:
        log.warning("Failed to fetch wind speed: %s", e)
        return 0.0


def _eaqi(pm25: float | None, pm10: float | None) -> int:
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
    return max(scores) if scores else 0


def _eaqi_label(score: int) -> str:
    return {1: "Velmi dobrá", 2: "Dobrá", 3: "Přijatelná", 4: "Špatná", 5: "Nezdravá"}.get(score, str(score))


def fetch_openaq() -> dict:
    """Fetch latest PM2.5 / PM10 from OPENAQ_STATION (ČHMÚ) via OpenAQ v3. Returns {} on failure."""
    try:
        r = requests.get(
            "https://api.openaq.org/v3/locations",
            params={"name": OPENAQ_STATION, "limit": 5},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        locations = r.json().get("results", [])
        if not locations:
            log.warning("OpenAQ: station %r not found", OPENAQ_STATION)
            return {}

        location_id = locations[0]["id"]
        log.info("OpenAQ: location id=%s name=%s", location_id, locations[0].get("name"))

        r = requests.get(
            f"https://api.openaq.org/v3/locations/{location_id}/latest",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()

        pm25 = pm10 = None
        for sensor in r.json().get("results", []):
            param = sensor.get("parameter", {})
            name  = param.get("name", "") if isinstance(param, dict) else str(param)
            value = sensor.get("value")
            if value is None:
                continue
            if name == "pm25":
                pm25 = round(float(value), 1)
            elif name == "pm10":
                pm10 = round(float(value), 1)

        out = {}
        if pm25 is not None:
            out["openaq_pm25"] = pm25
        if pm10 is not None:
            out["openaq_pm10"] = pm10
        score = _eaqi(pm25, pm10)
        if score:
            out["openaq_aqi"] = _eaqi_label(score)

        log.info("OpenAQ: pm25=%s pm10=%s aqi=%s", pm25, pm10, out.get("openaq_aqi"))
        return out
    except Exception as e:
        log.warning("OpenAQ fetch failed: %s", e)
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


def health_label(score: int, outdoor_temp: float) -> str:
    """Czech air quality label with ventilation advice based on outdoor temperature."""
    labels = {0: "Zdravý", 1: "Dobrý", 2: "Přijatelný", 3: "Špatný", 4: "Nezdravý"}
    label = labels.get(score, str(score))

    if score <= 1:
        return label

    if outdoor_temp < -10 or outdoor_temp > 35:
        vent = "větrejte krátce"
    elif score >= 4:
        vent = "větrejte ihned"
    else:
        vent = "větrejte"

    return f"{label} – {vent}"


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


def parse_homecoach_measurements(data: dict) -> dict:
    """
    Returns measurements keyed by room name (from HOMECOACH_MAP) or MAC suffix.
    Also returns a list of per-device dicts for LLM ventilation queries.
    """
    values = {}
    coaches = []

    for device in data.get("body", {}).get("devices", []):
        dash = device.get("dashboard_data", {})
        mac  = device.get("_id", "00:00:00:00:00:00")

        # Resolve room name from map, fall back to last 4 MAC chars
        room_raw = HOMECOACH_MAP.get(mac, mac.replace(":", "_")[-4:])
        slug     = _slugify(room_raw)
        log.info("Coach device: id=%s room=%s slug=%s", mac, room_raw, slug)

        entry = {"slug": slug, "room": room_raw}

        if "Temperature" in dash:
            values[f"netatmo_{slug}_temp"]     = round(dash["Temperature"], 1)
            entry["temp"] = round(dash["Temperature"], 1)
        if "Humidity" in dash:
            values[f"netatmo_{slug}_humidity"] = dash["Humidity"]
            entry["humidity"] = dash["Humidity"]
        if "CO2" in dash:
            values[f"netatmo_{slug}_co2"]      = dash["CO2"]
            entry["co2"] = dash["CO2"]
        if "Noise" in dash:
            values[f"netatmo_{slug}_noise"]    = dash["Noise"]
        if "health_idx" in dash:
            entry["health_idx"] = dash["health_idx"]

        coaches.append(entry)

    return values, coaches


def ask_llm(prompt: str) -> str:
    log.info("Asking LLM (%s)...", OLLAMA_MODEL)
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model":      OLLAMA_MODEL,
            "prompt":     prompt,
            "stream":     False,
            "think":      False,
            "keep_alive": -1,
            "options":    {"temperature": 0.3},
        },
        timeout=120,
    )
    r.raise_for_status()
    response = r.json()["response"].strip()
    log.info("LLM response: %s", response)
    return response


def ask_llm_ventilation(coach: dict, outdoor_temp: float) -> str:
    """Ask LLM whether to ventilate for a specific room/coach."""
    co2      = coach.get("co2", "?")
    humidity = coach.get("humidity", "?")
    room     = coach.get("room", "room")

    prompt = (
        f"{room} - interiér: CO2 {co2} ppm, vlhkost {humidity} %. "
        f"Exteriér: {outdoor_temp} °C. "
        "Mám větrat? Jedna věta, stručně, prakticky, česky."
    )
    return ask_llm(prompt)


def push_to_zo(values: dict):
    if not values:
        log.warning("No values to push")
        return
    params = {"import_key": ZO_IMPORT_KEY, **values}
    log.info("Pushing %d values to Zivy obraz...", len(values))
    r = requests.get("https://in.zivyobraz.eu/", params=params, timeout=10)
    r.raise_for_status()
    log.info("OK: %s", r.text.strip())


def main():
    try:
        token = get_access_token()

        station_data = fetch_station_data(token)
        coach_data   = fetch_homecoach_data(token)

        values = {}
        values.update(parse_measurements(station_data))

        coach_values, coaches = parse_homecoach_measurements(coach_data)
        values.update(coach_values)

        log.info("Measurements: %s", values)

        # Air quality from OpenAQ / ČHMÚ
        values.update(fetch_openaq())

        # Use Netatmo wind if available, otherwise fall back to Open-Meteo
        wind_kmh = float(values.get("netatmo_wind_speed", 0) or fetch_wind_speed())

        outdoor_temp     = values.get("netatmo_outdoor_temp", 20.0)
        outdoor_humidity = values.get("netatmo_outdoor_humidity", 50)
        if isinstance(outdoor_temp, (int, float)) and isinstance(outdoor_humidity, (int, float)):
            values["netatmo_outdoor_feels_like"] = feels_like(outdoor_temp, outdoor_humidity, wind_kmh)

        outdoor_temp = values.get("netatmo_outdoor_temp", 20.0)

        # Indoor module health index (computed from CO2 + humidity)
        co2 = values.get("netatmo_indoor_co2")
        hum = values.get("netatmo_indoor_humidity")
        if isinstance(co2, int) and isinstance(hum, int):
            values["netatmo_indoor_health"] = health_label(_health_score(co2, hum), outdoor_temp)

        # Home Coach health index (provided by Netatmo API)
        for coach in coaches:
            slug = coach["slug"]
            if "health_idx" in coach:
                values[f"netatmo_{slug}_health"] = health_label(coach["health_idx"], outdoor_temp)

        for coach in coaches:
            slug = coach["slug"]
            values[f"llm_vetrani_{slug}"] = ask_llm_ventilation(coach, outdoor_temp)

        push_to_zo(values)
    except requests.HTTPError as e:
        log.error("HTTP %s: %s", e.response.status_code, e.response.text)
        raise


if __name__ == "__main__":
    main()
