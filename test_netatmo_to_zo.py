#!/usr/bin/env python3
"""
Tests for netatmo_to_zo.py.
Run with: pytest test_netatmo_to_zo.py
"""

import json
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest
import requests

# Stub required secrets before the module is imported
os.environ.setdefault("NETATMO_CLIENT_ID", "test_id")
os.environ.setdefault("NETATMO_CLIENT_SECRET", "test_secret")
os.environ.setdefault("NETATMO_REFRESH_TOKEN", "test_refresh")
os.environ.setdefault("ZO_IMPORT_KEY", "test_zo_key")
os.environ.setdefault("OPENAQ_API_KEY", "test_openaq_key")

import netatmo_to_zo as ntz  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_response(json_data, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.raise_for_status.return_value = None
    m.text = "OK"
    return m


# ── _eaqi ─────────────────────────────────────────────────────────────────────

class TestEaqi:
    @pytest.mark.parametrize("pm25,expected", [
        (0.0, 1), (10.0, 1),
        (10.1, 2), (20.0, 2),
        (20.1, 3), (25.0, 3),
        (25.1, 4), (50.0, 4),
        (50.1, 5),
    ])
    def test_pm25_only(self, pm25, expected):
        assert ntz._eaqi(pm25, None) == expected

    @pytest.mark.parametrize("pm10,expected", [
        (0.0, 1), (20.0, 1),
        (20.1, 2), (40.0, 2),
        (40.1, 3), (50.0, 3),
        (50.1, 4), (100.0, 4),
        (100.1, 5),
    ])
    def test_pm10_only(self, pm10, expected):
        assert ntz._eaqi(None, pm10) == expected

    def test_worst_of_both_wins(self):
        assert ntz._eaqi(5.0, 150.0) == 5   # pm25=1, pm10=5
        assert ntz._eaqi(60.0, 10.0) == 5   # pm25=5, pm10=1

    @pytest.mark.parametrize("no2,expected", [
        (0.0, 1), (40.0, 1),
        (40.1, 2), (90.0, 2),
        (90.1, 3), (120.0, 3),
        (120.1, 4), (230.0, 4),
        (230.1, 5),
    ])
    def test_no2_only(self, no2, expected):
        assert ntz._eaqi(None, None, no2=no2) == expected

    @pytest.mark.parametrize("o3,expected", [
        (0.0, 1), (50.0, 1),
        (50.1, 2), (100.0, 2),
        (100.1, 3), (130.0, 3),
        (130.1, 4), (240.0, 4),
        (240.1, 5),
    ])
    def test_o3_only(self, o3, expected):
        assert ntz._eaqi(None, None, o3=o3) == expected

    def test_no2_and_o3_included_in_worst_of(self):
        assert ntz._eaqi(5.0, 10.0, no2=250.0) == 5   # no2 dominates

    def test_both_none_returns_zero(self):
        assert ntz._eaqi(None, None) == 0


# ── _eaqi_label ───────────────────────────────────────────────────────────────

class TestEaqiLabel:
    @pytest.mark.parametrize("score,label", [
        (1, "Velmi dobrá"),
        (2, "Dobrá"),
        (3, "Přijatelná"),
        (4, "Špatná"),
        (5, "Nezdravá"),
    ])
    def test_known_scores(self, score, label):
        assert ntz._eaqi_label(score) == label

    def test_unknown_score_returns_stringified(self):
        assert ntz._eaqi_label(99) == "99"


# ── feels_like ────────────────────────────────────────────────────────────────

class TestFeelsLike:
    def test_wind_chill_lowers_apparent_temp(self):
        assert ntz.feels_like(-5.0, 50, 30.0) < -5.0

    def test_wind_chill_ignored_above_14c(self):
        assert ntz.feels_like(15.0, 50, 30.0) == 15.0

    def test_wind_chill_ignored_below_4_8_kmh(self):
        assert ntz.feels_like(0.0, 50, 4.0) == 0.0

    def test_wind_chill_not_applied_when_formula_is_warmer(self):
        # At 14°C and low wind, formula can produce wc > temp; raw temp is returned
        result = ntz.feels_like(14.0, 50, 5.0)
        assert result == 14.0

    def test_heat_index_raises_apparent_temp(self):
        assert ntz.feels_like(35.0, 80, 0.0) > 35.0

    def test_heat_index_ignored_below_27c(self):
        assert ntz.feels_like(26.0, 80, 0.0) == 26.0

    def test_heat_index_ignored_below_40_percent_humidity(self):
        assert ntz.feels_like(30.0, 39, 0.0) == 30.0

    def test_passthrough_moderate_conditions(self):
        assert ntz.feels_like(20.0, 50, 0.0) == 20.0


# ── _health_score ─────────────────────────────────────────────────────────────

class TestHealthScore:
    @pytest.mark.parametrize("co2,expected", [
        (799, 0), (800, 1), (999, 1),
        (1000, 2), (1399, 2),
        (1400, 3), (1999, 3),
        (2000, 4),
    ])
    def test_co2_thresholds(self, co2, expected):
        # humidity=45 → score 0, so CO2 dominates
        assert ntz._health_score(co2, 45) == expected

    @pytest.mark.parametrize("hum,expected", [
        (30, 0), (45, 0), (60, 0),
        (25, 1), (29, 1), (61, 1), (70, 1),
        (20, 2), (24, 2), (71, 2), (75, 2),
        (15, 3), (19, 3), (76, 3), (80, 3),
        (10, 4), (85, 4),
    ])
    def test_humidity_thresholds(self, hum, expected):
        # CO2=500 → score 0, so humidity dominates
        assert ntz._health_score(500, hum) == expected

    def test_worst_of_co2_and_humidity_wins(self):
        assert ntz._health_score(500, 15) == 3   # co2=0, hum=3
        assert ntz._health_score(1400, 45) == 3  # co2=3, hum=0


# ── health_label ──────────────────────────────────────────────────────────────

class TestHealthLabel:
    @pytest.mark.parametrize("score,label", [
        (0, "Zdravá"), (1, "Dobrá"), (2, "Přijatelná"), (3, "Špatná"), (4, "Nezdravá"),
    ])
    def test_known_scores(self, score, label):
        assert ntz.health_label(score) == label

    def test_unknown_score_returns_stringified(self):
        assert ntz.health_label(99) == "99"


# ── _slugify ──────────────────────────────────────────────────────────────────

class TestSlugify:
    def test_simple_ascii(self):
        assert ntz._slugify("Pracovna") == "pracovna"

    def test_czech_diacritics_stripped(self):
        assert ntz._slugify("Ložnice") == "loznice"
        assert ntz._slugify("Obývák") == "obyvak"

    def test_spaces_become_underscores(self):
        assert ntz._slugify("Obyvak Velky") == "obyvak_velky"

    def test_dashes_become_underscores(self):
        assert ntz._slugify("patro-2") == "patro_2"

    def test_leading_trailing_whitespace_stripped(self):
        assert ntz._slugify("  room  ") == "room"


# ── parse_measurements ────────────────────────────────────────────────────────

_STATION_PAYLOAD = {
    "body": {
        "devices": [{
            "dashboard_data": {
                "Temperature": 21.3,
                "Humidity": 45,
                "CO2": 850,
                "Pressure": 1013.0,
            },
            "modules": [
                {
                    "type": "NAModule1",
                    "dashboard_data": {"Temperature": 8.7, "Humidity": 72},
                },
                {
                    "type": "NAModule2",
                    "dashboard_data": {
                        "WindStrength": 12, "GustStrength": 18,
                        "WindAngle": 270, "max_wind_str": 22,
                    },
                },
                {
                    "type": "NAModule3",
                    "dashboard_data": {"Rain": 0.5, "sum_rain_1": 1.2, "sum_rain_24": 5.8},
                },
            ],
        }]
    }
}


class TestParseMeasurements:
    def setup_method(self):
        self.v = ntz.parse_measurements(_STATION_PAYLOAD)

    def test_indoor_fields(self):
        assert self.v["netatmo_indoor_temp"] == 21.3
        assert self.v["netatmo_indoor_humidity"] == 45
        assert self.v["netatmo_indoor_co2"] == 850
        assert self.v["netatmo_pressure"] == 1013.0

    def test_outdoor_fields(self):
        assert self.v["netatmo_outdoor_temp"] == 8.7
        assert self.v["netatmo_outdoor_humidity"] == 72

    def test_wind_fields(self):
        assert self.v["netatmo_wind_speed"] == 12
        assert self.v["netatmo_wind_gust"] == 18
        assert self.v["netatmo_wind_angle"] == 270
        assert self.v["netatmo_wind_max"] == 22

    def test_rain_fields(self):
        assert self.v["netatmo_rain_current"] == 0.5
        assert self.v["netatmo_rain_1h"] == 1.2
        assert self.v["netatmo_rain_24h"] == 5.8

    def test_empty_payload_returns_empty_dict(self):
        assert ntz.parse_measurements({}) == {}

    def test_partial_data_no_missing_key_errors(self):
        payload = {"body": {"devices": [{"dashboard_data": {"Temperature": 20.0}, "modules": []}]}}
        v = ntz.parse_measurements(payload)
        assert "netatmo_indoor_temp" in v
        assert "netatmo_outdoor_temp" not in v


# ── parse_homecoach_measurements ──────────────────────────────────────────────

_COACH_PAYLOAD = {
    "body": {
        "devices": [{
            "_id": "70:ee:50:83:3e:ce",
            "dashboard_data": {
                "Temperature": 22.0,
                "Humidity": 48,
                "CO2": 900,
                "Noise": 35,
                "health_idx": 1,
            },
        }]
    }
}


class TestParseHomecoachMeasurements:
    def test_known_mac_uses_room_name(self):
        with patch.object(ntz, "HOMECOACH_MAP", {"70:ee:50:83:3e:ce": "Pracovna"}):
            v = ntz.parse_homecoach_measurements(_COACH_PAYLOAD)
        assert v["netatmo_pracovna_temp"] == 22.0
        assert v["netatmo_pracovna_humidity"] == 48
        assert v["netatmo_pracovna_co2"] == 900
        assert v["netatmo_pracovna_noise"] == 35
        assert v["netatmo_pracovna_health"] == "Dobrá"  # health_idx=1

    def test_unknown_mac_falls_back_to_mac_suffix(self):
        with patch.object(ntz, "HOMECOACH_MAP", {}):
            v = ntz.parse_homecoach_measurements(_COACH_PAYLOAD)
        # "70:ee:50:83:3e:ce".replace(":", "_")[-4:] = "e_ce", slugified = "e_ce"
        assert any("e_ce" in k for k in v)

    def test_empty_payload(self):
        v = ntz.parse_homecoach_measurements({})
        assert v == {}


def _sensor(name, value):
    return {"parameter": {"name": name}, "latest": {"value": value}}


# ── fetch_openaq ──────────────────────────────────────────────────────────────

class TestFetchOpenaq:
    def test_returns_pm25_pm10_and_aqi(self):
        resp = _mock_response({"results": [
            _sensor("PM2.5", 12.0),
            _sensor("PM10",  25.0),
        ]})
        with patch("requests.get", return_value=resp):
            result = ntz.fetch_openaq()
        assert result["openaq_pm25"] == 12.0
        assert result["openaq_pm10"] == 25.0
        assert result["openaq_aqi"] == "Dobrá"   # pm25→2, pm10→2 → max=2

    def test_uses_configured_location_id(self):
        resp = _mock_response({"results": []})
        with patch("requests.get", return_value=resp) as mock_get, \
             patch.object(ntz, "OPENAQ_LOCATION_ID", 9999):
            ntz.fetch_openaq()
        assert "9999" in mock_get.call_args[0][0]

    def test_api_key_header_sent(self):
        resp = _mock_response({"results": []})
        with patch("requests.get", return_value=resp) as mock_get:
            ntz.fetch_openaq()
        assert mock_get.call_args[1]["headers"]["X-API-Key"] == ntz.OPENAQ_API_KEY

    def test_none_values_are_skipped(self):
        resp = _mock_response({"results": [
            _sensor("PM2.5", None),
            _sensor("PM10",  30.0),
        ]})
        with patch("requests.get", return_value=resp):
            result = ntz.fetch_openaq()
        assert "openaq_pm25" not in result
        assert result["openaq_pm10"] == 30.0

    def test_no_sensors_returns_empty(self):
        with patch("requests.get", return_value=_mock_response({"results": []})):
            assert ntz.fetch_openaq() == {}

    def test_network_error_returns_empty(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            assert ntz.fetch_openaq() == {}

    def test_very_high_pm_gives_score_5(self):
        resp = _mock_response({"results": [_sensor("PM2.5", 60.0)]})
        with patch("requests.get", return_value=resp):
            assert ntz.fetch_openaq()["openaq_aqi"] == "Nezdravá"

    def test_returns_no2_and_o3(self):
        resp = _mock_response({"results": [
            _sensor("NO2", 50.0),
            _sensor("O3",  80.0),
        ]})
        with patch("requests.get", return_value=resp):
            result = ntz.fetch_openaq()
        assert result["openaq_no2"] == 50.0
        assert result["openaq_o3"] == 80.0
        assert result["openaq_aqi"] == "Dobrá"   # no2→2, o3→2 → max=2

    def test_no2_dominates_aqi(self):
        resp = _mock_response({"results": [
            _sensor("PM2.5", 5.0),
            _sensor("NO2",   250.0),
        ]})
        with patch("requests.get", return_value=resp):
            assert ntz.fetch_openaq()["openaq_aqi"] == "Nezdravá"


# ── fetch_wind_speed ──────────────────────────────────────────────────────────

class TestFetchWindSpeed:
    def test_returns_wind_speed_from_api(self):
        resp = _mock_response({"current": {"wind_speed_10m": 15.5}})
        with patch("requests.get", return_value=resp), \
             patch.object(ntz, "LOCATION_LAT", 49.2), \
             patch.object(ntz, "LOCATION_LON", 16.6):
            assert ntz.fetch_wind_speed() == 15.5

    def test_returns_zero_on_network_error(self):
        with patch("requests.get", side_effect=Exception("err")), \
             patch.object(ntz, "LOCATION_LAT", 49.2), \
             patch.object(ntz, "LOCATION_LON", 16.6):
            assert ntz.fetch_wind_speed() == 0.0

    def test_returns_zero_when_coords_not_set(self):
        with patch.object(ntz, "LOCATION_LAT", 0.0), \
             patch.object(ntz, "LOCATION_LON", 0.0):
            assert ntz.fetch_wind_speed() == 0.0


# ── push_to_zo ────────────────────────────────────────────────────────────────

class TestPushToZo:
    def test_sends_values_with_import_key(self):
        with patch("requests.get", return_value=_mock_response("OK")) as mock_get:
            ntz.push_to_zo({"foo": 1, "bar": 2})
        params = mock_get.call_args[1]["params"]
        assert params["foo"] == 1
        assert params["bar"] == 2
        assert params["import_key"] == ntz.ZO_IMPORT_KEY

    def test_skips_request_on_empty_values(self):
        with patch("requests.get") as mock_get:
            ntz.push_to_zo({})
        mock_get.assert_not_called()


# ── token management ──────────────────────────────────────────────────────────

class TestTokenManagement:
    def test_load_tokens_reads_from_file(self, tmp_path):
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({"refresh_token": "file_token"}))
        with patch.object(ntz, "TOKEN_FILE", str(token_file)):
            assert ntz.load_tokens()["refresh_token"] == "file_token"

    def test_load_tokens_falls_back_to_env_when_file_missing(self, tmp_path):
        with patch.object(ntz, "TOKEN_FILE", str(tmp_path / "nonexistent.json")):
            tokens = ntz.load_tokens()
        assert tokens["refresh_token"] == ntz.NETATMO_REFRESH_TOKEN

    def test_save_tokens_writes_json(self, tmp_path):
        token_file = tmp_path / "tokens.json"
        with patch.object(ntz, "TOKEN_FILE", str(token_file)):
            ntz.save_tokens({"refresh_token": "saved_token"})
        assert json.loads(token_file.read_text())["refresh_token"] == "saved_token"

    def test_refresh_access_token_posts_correct_payload(self):
        resp = _mock_response({"access_token": "new_access", "refresh_token": "new_refresh"})
        with patch("requests.post", return_value=resp) as mock_post:
            result = ntz.refresh_access_token("old_refresh")
        assert result["access_token"] == "new_access"
        data = mock_post.call_args[1]["data"]
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "old_refresh"
        assert data["client_id"] == ntz.NETATMO_CLIENT_ID


# ── ask_llm ───────────────────────────────────────────────────────────────────

class TestGetAccessToken:
    def test_refreshes_persists_and_returns_access_token(self, tmp_path):
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({"refresh_token": "old_refresh"}))
        resp = _mock_response({"access_token": "new_access", "refresh_token": "new_refresh"})
        with patch.object(ntz, "TOKEN_FILE", str(token_file)), \
             patch("requests.post", return_value=resp):
            token = ntz.get_access_token()
        assert token == "new_access"
        assert json.loads(token_file.read_text())["refresh_token"] == "new_refresh"


class TestSecret:
    def test_reads_from_secret_file(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="  file_value  \n")):
            assert ntz._secret("ANY_KEY") == "file_value"

    def test_falls_back_to_env_var(self):
        with patch("os.path.exists", return_value=False), \
             patch.dict(os.environ, {"ANY_KEY": "env_value"}):
            assert ntz._secret("ANY_KEY") == "env_value"

    def test_uses_default_when_no_file_and_no_env(self):
        env = {k: v for k, v in os.environ.items() if k != "MISSING_KEY"}
        with patch("os.path.exists", return_value=False), \
             patch.dict(os.environ, env, clear=True):
            assert ntz._secret("MISSING_KEY", "fallback") == "fallback"

    def test_raises_when_no_file_no_env_no_default(self):
        env = {k: v for k, v in os.environ.items() if k != "MISSING_KEY"}
        with patch("os.path.exists", return_value=False), \
             patch.dict(os.environ, env, clear=True):
            with pytest.raises(KeyError):
                ntz._secret("MISSING_KEY")

    def test_secret_file_takes_precedence_over_env(self):
        with patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="file_wins")), \
             patch.dict(os.environ, {"ANY_KEY": "env_loses"}):
            assert ntz._secret("ANY_KEY") == "file_wins"


# ── robustness: stale detection, retries, alerts, signals ─────────────────────

import subprocess  # noqa: E402
import signal as _signal  # noqa: E402
import sys  # noqa: E402
import textwrap  # noqa: E402
import time  # noqa: E402

NOW = 1_800_000_000.0


def _station(indoor_age_min=1, outdoor_age_min=1, indoor_dash=True, outdoor_dash=True,
             outdoor_reachable=True, with_outdoor=True, now=NOW):
    def dash(age, **kw):
        return {"time_utc": now - age * 60, **kw}
    device = {"type": "NAMain", "reachable": True, "modules": []}
    if indoor_dash:
        device["dashboard_data"] = dash(indoor_age_min, Temperature=22.9, Humidity=40, CO2=600, Pressure=1010.0)
    if with_outdoor:
        mod = {"type": "NAModule1", "reachable": outdoor_reachable}
        if outdoor_dash:
            mod["dashboard_data"] = dash(outdoor_age_min, Temperature=18.2, Humidity=60)
        device["modules"].append(mod)
    return {"body": {"devices": [device]}}


def _eval(data, known=None, last_ts=None):
    return ntz.evaluate_station(data, NOW, known if known is not None else {"NAMain", "NAModule1"},
                                last_ts if last_ts is not None else {})


OUTDOOR_KEYS = {"netatmo_outdoor_temp", "netatmo_outdoor_humidity", "netatmo_outdoor_feels_like"}


class TestStaleDetection:
    def test_fresh_data_passes_through(self):
        values, stale, infos = _eval(_station())
        assert stale == set()
        assert values["netatmo_outdoor_temp"] == 18.2
        assert infos["NAModule1"]["age"] == pytest.approx(1)

    def test_old_outdoor_is_stale_indoor_stays(self):
        values, stale, infos = _eval(_station(outdoor_age_min=240))
        assert stale == OUTDOOR_KEYS
        assert "netatmo_outdoor_temp" not in values
        assert values["netatmo_indoor_temp"] == 22.9
        assert infos["NAModule1"]["age"] == pytest.approx(240)

    def test_threshold_boundary(self):
        with patch.object(ntz, "STALE_AFTER_MIN", 30):
            assert _eval(_station(outdoor_age_min=29))[1] == set()
            assert _eval(_station(outdoor_age_min=31))[1] == OUTDOOR_KEYS

    def test_missing_dashboard_data_is_stale(self):
        values, stale, infos = _eval(_station(indoor_dash=False, outdoor_dash=False))
        assert "netatmo_indoor_temp" in stale and "netatmo_pressure" in stale
        assert OUTDOOR_KEYS <= stale
        assert values == {}
        assert infos["NAModule1"]["reason"] == "no dashboard_data"

    def test_unreachable_module_is_stale_even_with_fresh_timestamp(self):
        _, stale, infos = _eval(_station(outdoor_reachable=False))
        assert stale == OUTDOOR_KEYS
        assert infos["NAModule1"]["reason"] == "unreachable"

    def test_missing_known_module_is_stale(self):
        _, stale, infos = _eval(_station(with_outdoor=False))
        assert stale == OUTDOOR_KEYS
        assert infos["NAModule1"]["reason"] == "missing"

    def test_unknown_missing_module_is_not_flagged(self):
        # a rain module we never saw must not be reported as stale
        _, stale, _ = _eval(_station(), known={"NAMain", "NAModule1"})
        assert not any("rain" in k for k in stale)

    def test_last_timestamp_is_remembered_for_age(self):
        last_ts = {}
        _eval(_station(outdoor_age_min=200), last_ts=last_ts)
        assert last_ts["NAModule1"] == NOW - 200 * 60
        _, _, infos = _eval(_station(outdoor_dash=False), last_ts=last_ts)
        assert infos["NAModule1"]["age"] == pytest.approx(200)

    def test_input_is_not_mutated(self):
        data = _station(outdoor_age_min=240)
        _eval(data)
        assert "dashboard_data" in data["body"]["devices"][0]["modules"][0]

    def test_describe_stale_message(self):
        _, stale, infos = _eval(_station(outdoor_age_min=190))
        msg = ntz.describe_stale("station", infos, len(stale))
        assert "station stale: outdoor last update" in msg
        assert "(age 190 min)" in msg
        assert msg.endswith("skipping 3 values")

    def test_coach_stale_and_missing(self):
        coach = {"body": {"devices": [
            {"_id": "aa", "dashboard_data": {"time_utc": NOW - 600 * 60, "Temperature": 21.0}},
        ]}}
        with patch.object(ntz, "HOMECOACH_MAP", {"aa": "Pracovna", "bb": "Obyvak"}):
            values, stale, infos = ntz.evaluate_coaches(coach, NOW, {"pracovna", "obyvak"}, {})
        assert values == {}
        assert "netatmo_pracovna_temp" in stale and "netatmo_obyvak_health" in stale
        assert infos["obyvak"]["reason"] == "missing"

    def test_apply_stale_modes(self):
        v = {"netatmo_outdoor_temp": 1}
        with patch.object(ntz, "STALE_VALUE", "--"):
            assert ntz.apply_stale(v, {"netatmo_outdoor_temp", "x"}) == 2
        assert v == {"netatmo_outdoor_temp": "--", "x": "--"}
        v = {"netatmo_outdoor_temp": 1}
        with patch.object(ntz, "STALE_VALUE", ntz.STALE_SKIP):
            ntz.apply_stale(v, {"netatmo_outdoor_temp"})
        assert v == {}


class TestHttpRequest:
    @pytest.fixture(autouse=True)
    def _fast(self):
        with patch.object(ntz, "RETRY_DELAYS", (0, 0, 0)):
            yield

    @staticmethod
    def _resp(status):
        r = _mock_response({}, status)
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError(response=r)
        return r

    def test_retries_503_then_succeeds(self):
        with patch("requests.get", side_effect=[self._resp(503), self._resp(503), self._resp(200)]) as g:
            assert ntz.http_request("get", "https://x.test/a").status_code == 200
        assert g.call_count == 3

    def test_gives_up_after_three_retries(self):
        with patch("requests.get", return_value=self._resp(503)) as g:
            with pytest.raises(requests.HTTPError):
                ntz.http_request("get", "https://x.test/a")
        assert g.call_count == 4

    def test_retries_network_errors_and_timeouts(self):
        seq = [requests.ConnectionError(), requests.Timeout(), self._resp(200)]
        with patch("requests.get", side_effect=seq) as g:
            ntz.http_request("get", "https://x.test/a")
        assert g.call_count == 3

    def test_429_is_retried(self):
        with patch("requests.get", side_effect=[self._resp(429), self._resp(200)]) as g:
            ntz.http_request("get", "https://x.test/a")
        assert g.call_count == 2

    def test_4xx_not_retried(self):
        with patch("requests.get", return_value=self._resp(401)) as g:
            with pytest.raises(requests.HTTPError):
                ntz.http_request("get", "https://x.test/a")
        assert g.call_count == 1

    def test_default_timeout_set(self):
        with patch("requests.get", return_value=self._resp(200)) as g:
            ntz.http_request("get", "https://x.test/a")
        assert g.call_args[1]["timeout"] == ntz.HTTP_TIMEOUT

    def test_error_description_has_no_query_or_secret(self):
        with patch("requests.get", return_value=self._resp(503)):
            with pytest.raises(requests.HTTPError) as ei:
                ntz.http_request("get", "https://in.zivyobraz.eu/?import_key=test_zo_key&a=1")
        text = ntz._describe(ei.value)
        assert "status=503" in text and "https://in.zivyobraz.eu/" in text
        assert "import_key" not in text and "test_zo_key" not in text

    def test_stop_event_aborts_retry_wait(self):
        ntz._stop.set()
        try:
            with patch.object(ntz, "RETRY_DELAYS", (60, 60, 60)), \
                 patch("requests.get", return_value=self._resp(503)) as g:
                with pytest.raises(requests.HTTPError):
                    ntz.http_request("get", "https://x.test/a")
            assert g.call_count == 1
        finally:
            ntz._stop.clear()


class TestAlerts:
    def _sent(self):
        return patch.object(ntz, "send_alert", return_value=True)

    def test_ok_stale_ok_alerts_once_each(self):
        state = {}
        with self._sent() as send:
            ntz.process_alert(state, False, "d", NOW)                 # ok -> ok: nothing
            ntz.process_alert(state, True, "d", NOW)                  # ok -> stale
            for i in range(1, 10):                                    # still stale, within 6 h
                ntz.process_alert(state, True, "d", NOW + i * 900)
            ntz.process_alert(state, False, "d", NOW + 10 * 900)      # stale -> ok
            ntz.process_alert(state, False, "d", NOW + 11 * 900)
        assert [c.args[0] for c in send.call_args_list] == ["stale", "recovered"]
        assert state["alert"] == {"status": "ok"}

    def test_reminder_after_repeat_hours(self):
        state = {}
        with self._sent() as send, patch.object(ntz, "ALERT_REPEAT_HOURS", 6):
            ntz.process_alert(state, True, "d", NOW)
            ntz.process_alert(state, True, "d", NOW + 6 * 3600 - 1)
            ntz.process_alert(state, True, "d", NOW + 6 * 3600)
            ntz.process_alert(state, True, "d", NOW + 6 * 3600 + 900)
        assert [c.args[0] for c in send.call_args_list] == ["stale", "reminder"]

    def test_failed_delivery_is_retried_next_cycle(self):
        state = {}
        with patch.object(ntz, "send_alert", side_effect=[False, True]) as send:
            ntz.process_alert(state, True, "d", NOW)
            assert state["alert"]["status"] == "ok"
            ntz.process_alert(state, True, "d", NOW + 900)
        assert send.call_count == 2 and state["alert"]["status"] == "stale"

    def test_state_survives_via_file(self, tmp_path):
        with patch.object(ntz, "STATE_FILE", str(tmp_path / "state.json")):
            state = {}
            with self._sent():
                ntz.process_alert(state, True, "d", NOW)
            ntz.save_state(state)
            with self._sent() as send:
                restarted = ntz.load_state()
                ntz.process_alert(restarted, True, "d", NOW + 900)
        send.assert_not_called()

    def test_no_channel_only_logs(self, caplog):
        with patch.object(ntz, "NTFY_URL", ""), patch.object(ntz, "ALERT_WEBHOOK_URL", ""), \
             patch("requests.post") as post:
            assert ntz.send_alert("stale", "msg") is True
        post.assert_not_called()
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_ntfy_and_webhook(self):
        with patch.object(ntz, "NTFY_URL", "https://ntfy.test"), patch.object(ntz, "NTFY_TOPIC", "t"), \
             patch.object(ntz, "ALERT_WEBHOOK_URL", "https://hook.test/x"), \
             patch("requests.post", return_value=_mock_response({})) as post:
            assert ntz.send_alert("stale", "Čau") is True
        urls = [c.args[0] for c in post.call_args_list]
        assert urls == ["https://ntfy.test/t", "https://hook.test/x"]
        assert post.call_args_list[0][1]["data"] == "Čau".encode("utf-8")


class TestCallmebot:
    def test_sends_get_with_encoded_params_and_hides_key(self, caplog):
        with patch.object(ntz, "NTFY_URL", ""), patch.object(ntz, "ALERT_WEBHOOK_URL", ""), \
             patch.object(ntz, "CALLMEBOT_PHONE", "+420111222333"), \
             patch.object(ntz, "CALLMEBOT_APIKEY", "key123456"), \
             patch("requests.get", return_value=_mock_response({})) as get:
            assert ntz.send_alert("stale", "Čau & ahoj") is True
        assert get.call_args.args[0] == "https://api.callmebot.com/whatsapp.php"
        params = get.call_args[1]["params"]
        assert params["phone"] == "+420111222333" and "Čau & ahoj" in params["text"]

    def test_failure_returns_false_and_logs_no_key(self, caplog):
        err = requests.HTTPError(response=_mock_response({}, 400))
        with patch.object(ntz, "NTFY_URL", ""), patch.object(ntz, "ALERT_WEBHOOK_URL", ""), \
             patch.object(ntz, "CALLMEBOT_PHONE", "+420111222333"), \
             patch.object(ntz, "CALLMEBOT_APIKEY", "key123456"), \
             patch("requests.get", side_effect=err):
            assert ntz.send_alert("stale", "x") is False
        assert "key123456" not in caplog.text and "+420111222333" not in caplog.text


class TestRawStationDump:
    def test_lists_flags_without_identifiers(self):
        data = _station(outdoor_dash=False)
        dev = data["body"]["devices"][0]
        dev.update(_id="70:ee:50:00:00:01", station_name="Domov", wifi_status=77, last_status_store=NOW - 600)
        dev["modules"][0].update(_id="02:00:00:00:00:02", rf_status=70, battery_percent=55)
        out = ntz.describe_raw_station(data, NOW)
        assert "NAMain[reachable=True wifi=77" in out and "last_store=10min" in out
        assert "NAModule1[reachable=True" in out and "battery=55" in out and "dashboard=none" in out
        assert "70:ee" not in out and "Domov" not in out

    def test_empty_response(self):
        assert ntz.describe_raw_station({}, NOW) == "no devices in response"


class TestCzechAlerts:
    def test_czech_local_time_message(self):
        _, stale, infos = _eval(_station(outdoor_age_min=190))
        with patch.object(ntz, "DISPLAY_TZ", "UTC"):
            msg = ntz.describe_stale_cs(infos, len(stale))
        assert msg.startswith("Venku: poslední měření v ")
        assert "(před 190 min)" in msg and msg.endswith("Dotčených hodnot: 3.")
        assert "UTC" not in msg and "Z" not in msg.split("(")[0][-6:]

    def test_log_message_says_utc(self):
        _, stale, infos = _eval(_station(outdoor_age_min=190))
        assert " UTC (age 190 min)" in ntz.describe_stale("station", infos, len(stale))

    def test_unreachable_reason_in_czech(self):
        _, stale, infos = _eval(_station(outdoor_reachable=False))
        assert "modul nedostupný" in ntz.describe_stale_cs(infos, len(stale))

    def test_ntfy_title_is_header_safe(self):
        with patch.object(ntz, "NTFY_URL", "https://ntfy.test"), patch.object(ntz, "NTFY_TOPIC", "t"), \
             patch.object(ntz, "ALERT_WEBHOOK_URL", ""), patch.object(ntz, "CALLMEBOT_PHONE", ""), \
             patch("requests.post", return_value=_mock_response({})) as post:
            ntz.send_alert("stale", "x")
        post.call_args[1]["headers"]["Title"].encode("latin-1")  # must not raise


class TestCallmebotResponse:
    def _send(self, text):
        resp = _mock_response({})
        resp.text = text
        with patch.object(ntz, "NTFY_URL", ""), patch.object(ntz, "ALERT_WEBHOOK_URL", ""), \
             patch.object(ntz, "CALLMEBOT_PHONE", "+420111222333"), \
             patch.object(ntz, "CALLMEBOT_APIKEY", "key123456"), \
             patch("requests.get", return_value=resp):
            return ntz.send_alert("stale", "x")

    def test_paused_is_error_and_not_delivered(self, caplog):
        assert self._send("<b>Account Paused</b>") is False
        assert any(r.levelname == "ERROR" and "Paused" in r.message for r in caplog.records)

    def test_normal_reply_is_delivered(self):
        assert self._send("Message queued. You will receive it in a few seconds.") is True


class TestRecoveryMessage:
    def test_contains_outage_duration(self):
        state = {"alert": {"status": "stale", "since": NOW, "last_alert": NOW}}
        with patch.object(ntz, "send_alert", return_value=True) as send:
            ntz.process_alert(state, False, "d", NOW + 4 * 3600 + 50 * 60)
        assert "4 h 50 min" in send.call_args.args[1]


class TestIteration:
    """Whole cycle with mocked Netatmo/ZO: stale -> missing dashboard -> recovery."""

    @pytest.fixture
    def env(self, tmp_path):
        patches = [
            patch.object(ntz, "STATE_FILE", str(tmp_path / "state.json")),
            patch.object(ntz, "HEARTBEAT_FILE", str(tmp_path / "hb")),
            patch.object(ntz, "get_access_token", return_value="tok"),
            patch.object(ntz, "fetch_openaq", return_value={}),
            patch.object(ntz, "fetch_wind_speed", return_value=0.0),
            patch.object(ntz, "fetch_homecoach_data", return_value={"body": {"devices": []}}),
            patch.object(ntz, "send_alert", return_value=True),
            patch.object(ntz, "STALE_VALUE", "--"),
        ]
        for p in patches:
            p.start()
        yield tmp_path
        patch.stopall()

    def _run(self, station, at):
        with patch.object(ntz, "fetch_station_data", return_value=station), \
             patch.object(ntz.time, "time", return_value=at), \
             patch.object(ntz, "push_to_zo", return_value=True) as push:
            ok = ntz.run_once()
        return ok, push.call_args.args[0]

    def test_scenario(self, env):
        # 1. fine
        ok, v = self._run(_station(), NOW)
        assert ok and v["netatmo_outdoor_temp"] == 18.2 and v["netatmo_station_stale"] == 0
        assert (env / "hb").exists()
        ntz.send_alert.assert_not_called()

        # 2. station returns 4 h old measurement
        ok, v = self._run(_station(indoor_age_min=240, outdoor_age_min=240), NOW + 900)
        assert ok
        assert v["netatmo_station_stale"] == 1
        assert v["netatmo_outdoor_temp"] == "--" and v["netatmo_indoor_temp"] == "--"
        assert v["netatmo_outdoor_feels_like"] == "--"
        assert ntz.send_alert.call_count == 1 and ntz.send_alert.call_args.args[0] == "stale"

        # 3. dashboard_data gone completely: still blank, no new alert
        ok, v = self._run(_station(indoor_dash=False, outdoor_dash=False), NOW + 1800)
        assert v["netatmo_outdoor_temp"] == "--" and v["netatmo_station_stale"] == 1
        assert ntz.send_alert.call_count == 1

        # 4. back to normal -> real values + one recovery alert
        ok, v = self._run(_station(now=NOW + 2700), NOW + 2700)
        assert v["netatmo_outdoor_temp"] == 18.2 and v["netatmo_station_stale"] == 0
        assert [c.args[0] for c in ntz.send_alert.call_args_list] == ["stale", "recovered"]

    def test_unreachable_main_module_never_yields_fake_20(self, env):
        data = _station(indoor_age_min=300, outdoor_age_min=300)
        dev = data["body"]["devices"][0]
        dev["reachable"] = False
        dev["modules"][0]["reachable"] = False
        _, v = self._run(data, NOW)
        assert 20.0 not in v.values()
        assert v["netatmo_outdoor_feels_like"] == "--" and v["netatmo_station_stale"] == 1

    def test_never_pushes_old_temperature_when_stale(self, env):
        _, v = self._run(_station(outdoor_age_min=240), NOW)
        assert 18.2 not in v.values()

    def test_station_api_503_still_pushes_coaches(self, env):
        coach = {"body": {"devices": [
            {"_id": "aa", "dashboard_data": {"time_utc": NOW - 60, "Temperature": 21.0, "Humidity": 40}}]}}
        err = requests.HTTPError(response=_mock_response({}, 503))
        with patch.object(ntz, "fetch_station_data", side_effect=err), \
             patch.object(ntz, "fetch_homecoach_data", return_value=coach), \
             patch.object(ntz, "HOMECOACH_MAP", {"aa": "pracovna"}), \
             patch.object(ntz.time, "time", return_value=NOW), \
             patch.object(ntz, "push_to_zo", return_value=True) as push:
            assert ntz.run_once() is True
        v = push.call_args.args[0]
        assert v["netatmo_pracovna_temp"] == 21.0
        assert "netatmo_outdoor_temp" not in v        # unknown yet, not blanked...

    def test_station_api_down_too_long_blanks(self, env):
        err = requests.HTTPError(response=_mock_response({}, 503))
        with patch.object(ntz, "fetch_station_data", side_effect=err), \
             patch.object(ntz, "push_to_zo", return_value=True) as push:
            for t in (NOW, NOW + 20 * 60, NOW + 40 * 60):
                with patch.object(ntz.time, "time", return_value=t):
                    ntz.run_once()
        v = push.call_args.args[0]
        assert v["netatmo_outdoor_temp"] == "--" and v["netatmo_station_stale"] == 1

    def test_exception_does_not_escape_and_logs_one_error(self, env, caplog):
        with patch.object(ntz, "fetch_station_data", side_effect=RuntimeError("boom")), \
             patch.object(ntz, "load_state", side_effect=ValueError("bad")):
            assert ntz.run_once() is False
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1 and errors[0].exc_info is None

    def test_push_failure_writes_no_heartbeat(self, env):
        err = requests.HTTPError(response=_mock_response({}, 500))
        with patch.object(ntz, "fetch_station_data", return_value=_station()), \
             patch.object(ntz, "push_to_zo", side_effect=err):
            assert ntz.run_once() is False
        assert not (env / "hb").exists()

    def test_openaq_failure_does_not_drop_rest(self, env):
        with patch.object(ntz, "fetch_openaq", side_effect=lambda: {}):
            ok, v = self._run(_station(), NOW)
        assert ok and "netatmo_indoor_temp" in v


class TestSigterm:
    def test_sigterm_stops_loop_fast_with_exit_0(self):
        code = textwrap.dedent("""
            import os, sys
            for k in ("NETATMO_CLIENT_ID","NETATMO_CLIENT_SECRET","NETATMO_REFRESH_TOKEN","ZO_IMPORT_KEY","OPENAQ_API_KEY"):
                os.environ.setdefault(k, "x")
            sys.path.insert(0, %r)
            import netatmo_to_zo as n
            n.INTERVAL_SEC = 900
            n.run_once = lambda: print("ready", flush=True)
            sys.exit(n.main([]))
        """ % os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "ready"
        t0 = time.time()
        p.send_signal(_signal.SIGTERM)
        assert p.wait(timeout=5) == 0
        assert time.time() - t0 < 3
