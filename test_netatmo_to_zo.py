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
        assert v["netatmo_pracovna_health"] == "Dobrý"  # health_idx=1

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
