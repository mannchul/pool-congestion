"""Tests for the pool congestion estimation logic, scraper, and predictions."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

import main as _main_mod
import pytest
from fastapi.testclient import TestClient

from main import (
    _apply_levels,
    _build_historical_predictions,
    _chart_to_levels,
    _daily_trend,
    _estimate_congestion,
    _extract_chart_data,
    _extract_historical_data,
    _get_day_type,
    _get_default_forecast,
    _get_operating_hours,
    _hourly_forecast,
    _is_closed_day,
    _weekly_schedule,
    app,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_global_cache():
    """Reset all module-level caches in main.py before each test.

    Uses direct attribute assignment on the imported main module so that
    subsequent calls to functions like _chart_to_levels() see the reset state.
    """
    _main_mod._LIVE_CACHE = None
    _main_mod._LIVE_CACHE_TIME = None
    _main_mod._CHART_DATA.clear()
    _main_mod._CHART_PREDICTIONS.clear()
    _main_mod._CHART_PREDICTIONS_DATE = None
    _main_mod._HISTORICAL_PREDICTIONS.clear()
    _main_mod._HISTORICAL_DATE = None
    yield


# ── Helpers ─────────────────────────────────────────────────────────────────


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Create a datetime for testing."""
    return datetime(year, month, day, hour, minute)


def _as_bytes(text: str) -> bytes:
    """Encode a string as bytes (used for mock HTML data with non-ASCII chars)."""
    return text.encode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Scraper unit tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExtractChartData:
    """Tests for _extract_chart_data which parses Google Chart data from HTML."""

    def test_extracts_hourly_users_from_valid_html(self):
        """Should extract {hour: user_count} from arrayToDataTable HTML."""
        html = _as_bytes("""
        <script>
        google.visualization.arrayToDataTable([
          ['06시', 88, ''],
          ['07시', 29, ''],
          ['08시', 42, ''],
          ['09시', 22, ''],
          ['10시', 40, ''],
        ]);
        </script>""")
        result = _extract_chart_data(html)
        assert result is not None
        assert result == {6: 88, 7: 29, 8: 42, 9: 22, 10: 40}

    def test_returns_none_when_no_arraytodatatable(self):
        """Should return None when arrayToDataTable is not present."""
        html = b"<html><body>No chart data here</body></html>"
        assert _extract_chart_data(html) is None

    def test_returns_none_with_fewer_than_3_matches(self):
        """Should return None if there are fewer than 3 data points."""
        html = _as_bytes("""
        <script>
        google.visualization.arrayToDataTable([
          ['06시', 88, ''],
        ]);
        </script>""")
        assert _extract_chart_data(html) is None

    def test_handles_malformed_hours(self):
        """Should still extract data even if hour strings are non-standard."""
        html = _as_bytes("""
        <script>
        google.visualization.arrayToDataTable([
          ['06시', 88],
          ['07xxx', 29],
          ['8', 42],
        ]);
        </script>""")
        result = _extract_chart_data(html)
        assert result is not None
        assert 6 in result
        assert result[6] == 88

    def test_handles_all_operating_hours_weekday(self):
        """Should extract all 15 weekday operating hours (06~20)."""
        rows = "".join([f"['{h:02d}시', {u}, '']," for h, u in [
            (6, 88), (7, 29), (8, 42), (9, 22), (10, 40),
            (11, 5), (12, 4), (13, 39), (14, 16), (15, 22),
            (16, 20), (17, 17), (18, 2), (19, 1), (20, 1),
        ]])
        html = _as_bytes(f"""
        <script>google.visualization.arrayToDataTable([{rows}]);</script>
        """)
        result = _extract_chart_data(html)
        assert result is not None
        assert len(result) == 15
        assert result[6] == 88
        assert result[12] == 4
        assert result[20] == 1

    def test_returns_none_on_empty_data(self):
        """Should return None when the data table is empty."""
        html = _as_bytes("""<script>google.visualization.arrayToDataTable([]);</script>""")
        assert _extract_chart_data(html) is None


class TestExtractHistoricalData:
    """Tests for _extract_historical_data which parses 4-column history chart."""

    def test_extracts_historical_data_from_valid_html(self):
        """Should extract {hour: {today, yesterday, last_week}} from history page."""
        html = _as_bytes("""
        <script>
        google.visualization.arrayToDataTable([
          ['06시', 45, 52, 43],
          ['07시', 20, 30, 25],
          ['08시', 35, 40, 38],
        ]);
        </script>""")
        result = _extract_historical_data(html)
        assert result is not None
        assert result == {
            6: {"today": 45, "yesterday": 52, "last_week": 43},
            7: {"today": 20, "yesterday": 30, "last_week": 25},
            8: {"today": 35, "yesterday": 40, "last_week": 38},
        }

    def test_returns_none_when_no_arraytodatatable(self):
        """Should return None when arrayToDataTable is not present."""
        html = b"<html><body>No history chart</body></html>"
        assert _extract_historical_data(html) is None

    def test_returns_none_with_too_few_rows(self):
        """Should return None if fewer than 3 rows exist."""
        html = _as_bytes("""
        <script>
        google.visualization.arrayToDataTable([
          ['06시', 45, 52, 43],
        ]);
        </script>""")
        assert _extract_historical_data(html) is None

    def test_handles_larger_user_counts(self):
        """Should handle larger numbers (hundreds of users)."""
        html = _as_bytes("""
        <script>
        google.visualization.arrayToDataTable([
          ['10시', 156, 134, 142],
          ['11시', 98, 112, 105],
          ['12시', 45, 52, 48],
        ]);
        </script>""")
        result = _extract_historical_data(html)
        assert result is not None
        assert result[10] == {"today": 156, "yesterday": 134, "last_week": 142}
        assert result[12]["today"] == 45


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Prediction function tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestChartToLevels:
    """Tests for _chart_to_levels which converts user counts to congestion %."""

    def test_converts_chart_data_to_levels_with_valid_cache(self, reset_global_cache):
        """Should calibrate chart user counts using live cache level."""
        _main_mod._LIVE_CACHE = {"level": 38}
        chart_data = {6: 88, 7: 29, 8: 42}
        now = _dt(2026, 6, 2, 8, 0)
        result = _chart_to_levels(chart_data, now)
        assert result is not None
        # factor = 38 / 42 ≈ 0.905
        # hour 6: round(88 * 0.905) = round(79.6) = 80
        # hour 7: round(29 * 0.905) = round(26.2) = 26
        # hour 8: round(42 * 0.905) = round(38.0) = 38
        assert result[6] == 80
        assert result[7] == 26
        assert result[8] == 38

    def test_falls_back_to_previous_hour_when_current_hour_zero(self, reset_global_cache):
        """Should try previous hour when current hour has 0 users."""
        _main_mod._LIVE_CACHE = {"level": 38}
        chart_data = {6: 88, 7: 0, 8: 42}
        now = _dt(2026, 6, 2, 7, 0)  # current hour 7 has 0 users
        result = _chart_to_levels(chart_data, now)
        assert result is not None
        # Should fall back to hour 6 (88 users)
        assert 6 in result

    def test_returns_none_when_no_live_cache(self, reset_global_cache):
        """Should return None if live cache is not set."""
        chart_data = {6: 88, 7: 29}
        now = _dt(2026, 6, 2, 8, 0)
        assert _chart_to_levels(chart_data, now) is None

    def test_returns_none_when_current_hour_and_previous_hours_zero(self, reset_global_cache):
        """Should return None if no valid calibration hour exists."""
        _main_mod._LIVE_CACHE = {"level": 38}
        chart_data = {6: 0, 7: 0, 8: 0}
        now = _dt(2026, 6, 2, 8, 0)
        assert _chart_to_levels(chart_data, now) is None

    def test_skips_zero_user_hours_in_output(self, reset_global_cache):
        """Should skip hours where users==0 (future hours)."""
        _main_mod._LIVE_CACHE = {"level": 38}
        chart_data = {6: 88, 7: 29, 8: 42, 9: 0, 10: 0}
        now = _dt(2026, 6, 2, 8, 0)
        result = _chart_to_levels(chart_data, now)
        assert result is not None
        assert 9 not in result  # zero hours excluded
        assert 10 not in result

    def test_clamps_levels_between_0_and_95(self, reset_global_cache):
        """Should clamp levels to 0-95 range."""
        _main_mod._LIVE_CACHE = {"level": 95}
        chart_data = {6: 88, 7: 10}
        now = _dt(2026, 6, 2, 8, 0)
        result = _chart_to_levels(chart_data, now)
        assert result is not None
        assert all(0 <= v <= 95 for v in result.values())

    def test_no_negative_levels(self, reset_global_cache):
        """Should never produce negative levels."""
        _main_mod._LIVE_CACHE = {"level": 5}
        chart_data = {6: 10, 7: 5}
        now = _dt(2026, 6, 2, 8, 0)
        result = _chart_to_levels(chart_data, now)
        assert result is not None
        assert all(v >= 0 for v in result.values())


class TestBuildHistoricalPredictions:
    """Tests for _build_historical_predictions with historical data calibration."""

    def test_builds_predictions_from_last_week_for_weekday(self, reset_global_cache):
        """Should use last_week data for weekday calibration."""
        _main_mod._LIVE_CACHE = {"level": 38}
        hist_data = {
            6: {"today": 45, "yesterday": 52, "last_week": 43},
            7: {"today": 20, "yesterday": 30, "last_week": 25},
            8: {"today": 35, "yesterday": 40, "last_week": 38},
        }
        now = _dt(2026, 6, 2, 8, 0)  # Tuesday
        result = _build_historical_predictions(hist_data, now)
        assert result is not None
        # factor = 38 / 38 = 1.0 (exact match at hour 8 last_week)
        assert result[8] == 38

    def test_returns_none_for_holiday(self, reset_global_cache):
        """Should return None for holiday days."""
        # July 5, 2026 is a Sunday — first Sunday of July → closed
        now = _dt(2026, 7, 5, 10, 0)  # First Sunday → closed
        hist_data = {
            10: {"today": 56, "yesterday": 0, "last_week": 50},
            11: {"today": 41, "yesterday": 0, "last_week": 40},
        }
        result = _build_historical_predictions(hist_data, now)
        assert result is None

    def test_returns_none_when_no_live_cache(self, reset_global_cache):
        """Should return None when live cache is not set."""
        hist_data = {
            6: {"today": 45, "yesterday": 52, "last_week": 43},
        }
        now = _dt(2026, 6, 2, 8, 0)
        assert _build_historical_predictions(hist_data, now) is None

    def test_falls_back_to_yesterday_last_week_today_chain(self, reset_global_cache):
        """Should try last_week → yesterday → today in order."""
        _main_mod._LIVE_CACHE = {"level": 38}
        # Current hour 8 has 0 in last_week, but non-zero in yesterday
        hist_data = {
            8: {"today": 35, "yesterday": 40, "last_week": 0},
            9: {"today": 20, "yesterday": 25, "last_week": 22},
        }
        now = _dt(2026, 6, 2, 8, 0)
        result = _build_historical_predictions(hist_data, now)
        assert result is not None
        # Should use yesterday=40 as calibration source
        assert result[8] is not None

    def test_calibration_factor_clamped_03_30(self, reset_global_cache):
        """Should clamp calibration factor to [0.3, 3.0] range."""
        _main_mod._LIVE_CACHE = {"level": 38}
        # Very small last_week value should produce factor 3.0 max
        hist_data = {
            6: {"today": 88, "yesterday": 0, "last_week": 2},
            7: {"today": 29, "yesterday": 0, "last_week": 5},
        }
        now = _dt(2026, 6, 2, 8, 0)
        result = _build_historical_predictions(hist_data, now)
        # factor = 38/2 = 19, clamped to 3.0
        # hour 6: 2 * 3.0 = 6
        if result:
            assert result[6] == 6

    def test_returns_none_when_all_data_is_zero(self, reset_global_cache):
        """Should return None when all historical data is zero."""
        _main_mod._LIVE_CACHE = {"level": 38}
        hist_data = {
            8: {"today": 0, "yesterday": 0, "last_week": 0},
        }
        now = _dt(2026, 6, 2, 8, 0)
        assert _build_historical_predictions(hist_data, now) is None


class TestGetDefaultForecast:
    """Tests for _get_default_forecast which uses known patterns."""

    def test_returns_weekday_forecast(self):
        """Should return levels for all weekday operating hours."""
        now = _dt(2026, 6, 2, 10, 0)  # Tuesday 10:00
        result = _get_default_forecast(now)
        assert result is not None
        # Weekday has hours 6-20
        assert 6 in result
        assert 20 in result
        # All values should be in valid range
        assert all(0 <= v <= 95 for v in result.values())
        # Hour 6 should be highest (morning peak)
        assert result[6] > result[10]

    def test_returns_saturday_forecast(self):
        """Should return levels for Saturday operating hours."""
        now = _dt(2026, 6, 6, 10, 0)  # Saturday 10:00
        result = _get_default_forecast(now)
        assert result is not None
        assert 6 in result
        assert 17 in result
        assert all(0 <= v <= 95 for v in result.values())

    def test_returns_sunday_forecast(self):
        """Should return levels for Sunday operating hours."""
        now = _dt(2026, 6, 14, 10, 0)  # Sunday 10:00 (2nd Sunday = open)
        result = _get_default_forecast(now)
        assert result is not None
        assert 10 in result
        assert 17 in result
        assert all(0 <= v <= 95 for v in result.values())

    def test_forecast_maintains_relative_pattern(self):
        """The relative pattern should be preserved (peak hours > quiet hours)."""
        now = _dt(2026, 6, 2, 10, 0)  # Tuesday
        result = _get_default_forecast(now)
        assert result is not None
        # Morning peak (06:00) should be higher than late evening (19:00)
        assert result[6] > result[19]
        # Afternoon should be lower than morning peak
        assert result[6] > result[14]

    def test_uses_fallback_calibration_when_current_hour_has_small_value(self):
        """When current hour has tiny value, should fall back to nearby significant hour."""
        # For weekday at hour 11 (val=5 in pattern, too small)
        now = _dt(2026, 6, 2, 11, 0)
        result = _get_default_forecast(now)
        assert result is not None
        # Should have successfully calibrated (fell back to 10 or 06)
        assert any(v > 10 for v in result.values())

    def test_returns_none_for_holiday(self):
        """Should return None for holiday since no pattern exists."""
        # First Sunday of May = May 3, 2026 (Sunday, day <= 7)
        now = _dt(2026, 5, 3, 10, 0)
        result = _get_default_forecast(now)
        # May 3 is the first Sunday → closed day → holiday day type → no pattern
        assert result is None


class TestApplyLevels:
    """Tests for _apply_levels which overrides forecast/trend with predictions."""

    def _make_forecast(self, *hour_level_pairs: tuple) -> List[Dict]:
        """Helper to create a forecast list from (hour, level) pairs."""
        items = []
        for hour, level in hour_level_pairs:
            items.append({
                "hour": f"{hour:02d}:00",
                "level": level,
                "label": "보통",
                "color": "#eab308",
            })
        return items

    def test_applies_prediction_levels(self):
        """Should replace original levels with prediction levels."""
        forecast = self._make_forecast((6, 55), (7, 55), (8, 45))
        predictions = {6: 60, 7: 50, 8: 40}
        _apply_levels(forecast, predictions)
        assert forecast[0]["level"] == 60  # hour 6: 55→60 (within delta=20)
        assert forecast[1]["level"] == 50  # hour 7: 55→50
        assert forecast[2]["level"] == 40  # hour 8: 45→40

    def test_clamps_to_max_delta(self):
        """Should clamp predicted level to original +/- max_delta."""
        forecast = self._make_forecast((6, 55), (10, 40))
        predictions = {6: 95, 10: 5}  # Extreme deviations
        _apply_levels(forecast, predictions, max_delta=20)
        # 55 → min(95, 55+20) = 75
        assert forecast[0]["level"] == 75
        # 40 → max(5, 40-20) = 20
        assert forecast[1]["level"] == 20

    def test_updates_label_and_color(self):
        """Should update label and color to match clamped level."""
        forecast = self._make_forecast((6, 55))
        predictions = {6: 65}  # Clamped to 65 → "혼잡" / orange (50 ≤ 65 < 70)
        _apply_levels(forecast, predictions, max_delta=20)
        assert forecast[0]["level"] == 65
        assert forecast[0]["label"] == "혼잡"
        assert forecast[0]["color"] == "#f97316"

    def test_skips_unmatched_hours(self):
        """Should leave forecast items unchanged when hour not in predictions."""
        forecast = self._make_forecast((6, 55), (7, 55), (8, 45))
        predictions = {6: 60, 10: 40}  # 7 and 8 not in predictions
        _apply_levels(forecast, predictions)
        assert forecast[0]["level"] == 60  # updated
        assert forecast[1]["level"] == 55  # unchanged
        assert forecast[2]["level"] == 45  # unchanged

    def test_handles_empty_predictions(self):
        """Should not change anything when predictions dict is empty."""
        forecast = self._make_forecast((6, 55))
        _apply_levels(forecast, {})
        assert forecast[0]["level"] == 55

    @pytest.mark.parametrize("pred_level,expected", [
        (-20, 0),   # below 0 → clamped to 0 → 여유/green
        (200, 100), # above 100 → clamped to 100 → 매우혼잡/red
    ])
    def test_clamps_to_0_and_100(self, pred_level, expected):
        """Should clamp final level to 0-100 range regardless of delta."""
        forecast = self._make_forecast((6, 55))
        predictions = {6: pred_level}
        _apply_levels(forecast, predictions, max_delta=100)
        assert forecast[0]["level"] == expected
        if expected == 0:
            assert forecast[0]["label"] == "여유"
            assert forecast[0]["color"] == "#22c55e"
        else:
            assert forecast[0]["label"] == "매우혼잡"
            assert forecast[0]["color"] == "#ef4444"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Helper function tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsClosedDay:
    """Tests for _is_closed_day closure detection."""

    @pytest.mark.parametrize("dt_args,expected_reason", [
        # First Sundays of months
        ((2026, 5, 3, 10, 0), True),   # May 3 = 1st Sunday
        ((2026, 6, 7, 10, 0), True),   # Jun 7 = 1st Sunday
        ((2026, 7, 5, 10, 0), True),   # Jul 5 = 1st Sunday
        # Third Sundays of months
        ((2026, 5, 17, 10, 0), True),  # May 17 = 3rd Sunday
        ((2026, 6, 21, 10, 0), True),  # Jun 21 = 3rd Sunday
        # Regular Sundays (2nd, 4th, 5th) — open
        ((2026, 5, 10, 10, 0), False), # May 10 = 2nd Sunday
        ((2026, 5, 24, 10, 0), False), # May 24 = 4th Sunday
        ((2026, 5, 31, 10, 0), False), # May 31 = 5th Sunday
        # Weekdays — open
        ((2026, 6, 1, 10, 0), False),  # Monday
        ((2026, 6, 2, 10, 0), False),  # Tuesday
        ((2026, 6, 3, 10, 0), False),  # Wednesday
        ((2026, 6, 4, 10, 0), False),  # Thursday
        ((2026, 6, 5, 10, 0), False),  # Friday
        # Saturday — open
        ((2026, 6, 6, 10, 0), False),  # Saturday
    ])
    def test_regular_closure_patterns(self, dt_args, expected_reason):
        """First/third Sunday closed, other days open."""
        now = _dt(*dt_args)
        result = _is_closed_day(now)
        if expected_reason:
            assert result is not None, f"Expected closed, got None for {dt_args}"
        else:
            assert result is None, f"Expected open, got {result!r} for {dt_args}"

    @pytest.mark.parametrize("dt_args,expected_reason", [
        ((2026, 2, 17, 10, 0), "설날"),  # Seollal 2026
        ((2026, 2, 18, 10, 0), "설날"),  # Seollal 2026
        ((2026, 9, 25, 10, 0), "추석"),  # Chuseok 2026
        ((2026, 9, 26, 10, 0), "추석"),  # Chuseok 2026
        ((2027, 2, 6, 10, 0), "설날"),   # Seollal 2027
        ((2027, 9, 14, 10, 0), "추석"),  # Chuseok 2027
    ])
    def test_specific_holiday_dates(self, dt_args, expected_reason):
        """Specific Seollal/Chuseok dates should be closed."""
        now = _dt(*dt_args)
        result = _is_closed_day(now)
        assert result is not None
        assert expected_reason in result

    def test_returns_string_for_closed_days(self):
        """Closed days should return a descriptive string."""
        now = _dt(2026, 5, 3, 10, 0)  # First Sunday
        result = _is_closed_day(now)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_none_for_open_days(self):
        """Open days should return None."""
        now = _dt(2026, 6, 2, 10, 0)  # Tuesday
        assert _is_closed_day(now) is None


class TestGetDayType:
    """Tests for _get_day_type classification."""

    @pytest.mark.parametrize("dt_args,expected", [
        ((2026, 6, 1, 10, 0), "weekday"),  # Monday
        ((2026, 6, 2, 10, 0), "weekday"),  # Tuesday
        ((2026, 6, 3, 10, 0), "weekday"),  # Wednesday
        ((2026, 6, 4, 10, 0), "weekday"),  # Thursday
        ((2026, 6, 5, 10, 0), "weekday"),  # Friday
        ((2026, 6, 6, 10, 0), "saturday"), # Saturday
        ((2026, 6, 14, 10, 0), "sunday"),  # Sunday (2nd Sunday = open)
        ((2026, 5, 3, 10, 0), "holiday"),  # 1st Sunday = closed
        ((2026, 2, 17, 10, 0), "holiday"), # Seollal
    ])
    def test_day_type_classification(self, dt_args, expected):
        """Should correctly classify each day type."""
        now = _dt(*dt_args)
        assert _get_day_type(now) == expected


class TestGetOperatingHours:
    """Tests for _get_operating_hours."""

    @pytest.mark.parametrize("dt_args,expected_start,expected_end", [
        ((2026, 6, 1, 10, 0), 6, 20),   # Monday
        ((2026, 6, 5, 10, 0), 6, 20),   # Friday
        ((2026, 6, 6, 10, 0), 6, 17),   # Saturday
        ((2026, 6, 14, 10, 0), 10, 17),  # Sunday (2nd Sunday = open)
    ])
    def test_operating_hours(self, dt_args, expected_start, expected_end):
        """Should return correct operating hours for each day type."""
        now = _dt(*dt_args)
        start, end = _get_operating_hours(now)
        assert start == expected_start
        assert end == expected_end

    def test_closed_day_returns_zero_zero(self):
        """Should return (0, 0) for closed days."""
        now = _dt(2026, 5, 3, 10, 0)  # First Sunday = closed
        assert _get_operating_hours(now) == (0, 0)


class TestWeeklySchedule:
    """Tests for _weekly_schedule."""

    def test_returns_7_days(self):
        """Should always return 7 days."""
        now = _dt(2026, 6, 2, 10, 0)  # Tuesday
        schedule = _weekly_schedule(now)
        assert len(schedule) == 7

    def test_first_day_is_today(self):
        """First entry should have is_today=True."""
        now = _dt(2026, 6, 2, 10, 0)
        schedule = _weekly_schedule(now)
        assert schedule[0]["is_today"] is True
        assert all(s["is_today"] is False for s in schedule[1:])

    def test_contains_required_fields(self):
        """Each day should have all required fields."""
        now = _dt(2026, 6, 2, 10, 0)
        schedule = _weekly_schedule(now)
        required = {"date", "day_name", "is_today", "is_closed", "closed_reason",
                     "hours", "status", "peak_level", "peak_label", "peak_color"}
        for day in schedule:
            missing = required - set(day.keys())
            assert not missing, f"Missing fields: {missing}"

    def test_closed_days_have_peak_level_zero(self):
        """Closed days should have peak_level=0 and status='휴장'."""
        # April 27, 2026 = Monday; includes May 3 (1st Sunday = closed)
        now = _dt(2026, 4, 27, 10, 0)
        schedule = _weekly_schedule(now)
        closed_days = [d for d in schedule if d["is_closed"]]
        assert len(closed_days) > 0
        for d in closed_days:
            assert d["peak_level"] == 0
            assert d["status"] == "휴장"
            assert d["hours"] == "--:--~--:--"

    def test_open_days_have_peak_level_above_zero(self):
        """Open days should have positive peak_level."""
        now = _dt(2026, 6, 1, 10, 0)  # Monday
        schedule = _weekly_schedule(now)
        open_days = [d for d in schedule if not d["is_closed"]]
        assert len(open_days) > 0
        for d in open_days:
            assert d["peak_level"] > 0
            assert d["status"] == "운영"
            assert ":00~" in d["hours"]

    def test_korean_day_names(self):
        """Day names should be in Korean."""
        now = _dt(2026, 6, 2, 10, 0)  # Tuesday
        schedule = _weekly_schedule(now)
        day_names = [d["day_name"] for d in schedule]
        expected = ["화", "수", "목", "금", "토", "일", "월"]
        assert day_names == expected

    def test_peak_level_is_reasonable(self):
        """Peak levels should be in 0-100 range."""
        now = _dt(2026, 6, 1, 10, 0)
        schedule = _weekly_schedule(now)
        for d in schedule:
            assert 0 <= d["peak_level"] <= 100


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Weekday / Saturday / Sunday estimation tests (existing)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWeekday:
    """0=Mon … 4=Fri — all use the same weekday schedule."""

    @pytest.mark.parametrize(
        "hour, minute, expected_level, expected_label",
        [
            (0, 0, 10, "여유"),
            (5, 59, 10, "여유"),
            (6, 0, 55, "보통"),
            (7, 0, 55, "보통"),
            (7, 59, 55, "보통"),
            (8, 0, 45, "보통"),
            (9, 30, 45, "보통"),
            (10, 0, 40, "보통"),
            (10, 59, 40, "보통"),
            (11, 0, 12, "여유"),
            (11, 30, 12, "여유"),
            (11, 59, 12, "여유"),
            (12, 0, 0, "수질정화시간"),
            (12, 30, 0, "수질정화시간"),
            (12, 59, 0, "수질정화시간"),
            (13, 0, 45, "보통"),
            (14, 0, 25, "여유"),
            (14, 59, 25, "여유"),
            (15, 0, 22, "여유"),
            (15, 59, 22, "여유"),
            (16, 0, 28, "여유"),
            (17, 0, 28, "여유"),
            (17, 59, 28, "여유"),
            (18, 0, 12, "여유"),
            (19, 0, 12, "여유"),
            (20, 0, 12, "여유"),
            (20, 1, 10, "여유"),
            (21, 0, 10, "여유"),
            (23, 59, 10, "여유"),
        ],
    )
    def test_all_slots(self, hour, minute, expected_level, expected_label):
        r = _estimate_congestion(_dt(2026, 5, 25, hour, minute))  # Monday
        assert r["level"] == expected_level, f"{hour:02d}:{minute:02d}"
        assert r["label"] == expected_label

    def test_day_type(self):
        for wd in range(5):
            dt = _dt(2026, 5, 25 + wd, 10, 0)
            assert _estimate_congestion(dt)["day_type"] == "평일"

    def test_is_weekend_false(self):
        for wd in range(5):
            dt = _dt(2026, 5, 25 + wd, 10, 0)
            assert _estimate_congestion(dt)["is_weekend"] is False


class TestSaturday:
    @pytest.mark.parametrize(
        "hour, minute, expected_level, expected_label",
        [
            (0, 0, 10, "여유"),
            (5, 59, 10, "여유"),
            (6, 0, 25, "여유"),
            (7, 0, 25, "여유"),
            (7, 59, 25, "여유"),
            (8, 0, 45, "보통"),
            (9, 0, 45, "보통"),
            (9, 59, 45, "보통"),
            (10, 0, 65, "보통"),
            (11, 0, 65, "보통"),
            (11, 59, 65, "보통"),
            (12, 0, 80, "혼잡"),
            (13, 0, 80, "혼잡"),
            (13, 59, 80, "혼잡"),
            (14, 0, 55, "보통"),
            (15, 0, 55, "보통"),
            (16, 0, 55, "보통"),
            (17, 0, 55, "보통"),
            (17, 1, 10, "여유"),
            (18, 0, 10, "여유"),
            (23, 59, 10, "여유"),
        ],
    )
    def test_all_slots(self, hour, minute, expected_level, expected_label):
        r = _estimate_congestion(_dt(2026, 5, 30, hour, minute))  # Saturday
        assert r["level"] == expected_level, f"{hour:02d}:{minute:02d}"
        assert r["label"] == expected_label

    def test_day_type(self):
        dt = _dt(2026, 5, 30, 10, 0)
        assert _estimate_congestion(dt)["day_type"] == "토요일"

    def test_is_weekend_true(self):
        dt = _dt(2026, 5, 30, 10, 0)
        assert _estimate_congestion(dt)["is_weekend"] is True


class TestSunday:
    @pytest.mark.parametrize(
        "hour, minute, expected_level, expected_label",
        [
            (0, 0, 10, "여유"),
            (9, 0, 10, "여유"),
            (9, 59, 10, "여유"),
            (10, 0, 45, "보통"),
            (10, 30, 45, "보통"),
            (10, 59, 45, "보통"),
            (11, 0, 40, "보통"),
            (11, 30, 40, "보통"),
            (11, 59, 40, "보통"),
            (12, 0, 20, "여유"),
            (12, 30, 20, "여유"),
            (12, 59, 20, "여유"),
            (13, 0, 55, "보통"),
            (14, 0, 55, "보통"),
            (14, 59, 55, "보통"),
            (15, 0, 65, "보통"),
            (15, 30, 65, "보통"),
            (15, 59, 65, "보통"),
            (16, 0, 25, "여유"),
            (17, 0, 25, "여유"),
            (17, 1, 10, "여유"),
            (18, 0, 10, "여유"),
            (23, 59, 10, "여유"),
        ],
    )
    def test_all_slots(self, hour, minute, expected_level, expected_label):
        r = _estimate_congestion(_dt(2026, 5, 31, hour, minute))  # Sunday
        assert r["level"] == expected_level, f"{hour:02d}:{minute:02d}"
        assert r["label"] == expected_label

    def test_day_type(self):
        dt = _dt(2026, 5, 31, 10, 0)
        assert _estimate_congestion(dt)["day_type"] == "일요일·공휴일"

    def test_is_weekend_true(self):
        dt = _dt(2026, 5, 31, 10, 0)
        assert _estimate_congestion(dt)["is_weekend"] is True


# ── Colour mapping tests ─────────────────────────────────────────────────────


class TestColourMapping:
    def test_green_below_30(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 15, 0))
        assert r["color"] == "#22c55e"

    def test_yellow_30_to_49(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 10, 30))
        assert r["color"] == "#eab308"

    def test_orange_50_to_69(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 6, 30))
        assert r["color"] == "#f97316"

    def test_red_70_and_above(self):
        r = _estimate_congestion(_dt(2026, 5, 30, 12, 30))
        assert r["color"] == "#ef4444"

    def test_break_time_zero_is_green(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 12, 30))
        assert r["color"] == "#22c55e"


# ── New fields tests ─────────────────────────────────────────────────────────


class TestNewFields:
    """Test recently added fields: male_rate, female_rate, status."""

    def test_male_rate_present(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 10, 0))
        assert "male_rate" in r
        assert isinstance(r["male_rate"], int)

    def test_female_rate_present(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 10, 0))
        assert "female_rate" in r
        assert isinstance(r["female_rate"], int)

    def test_status_present(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 10, 0))
        assert "status" in r
        assert r["status"] in ("운영중", "수질정화시간", "운영종료")

    @pytest.mark.parametrize(
        "dt_args, expected_status",
        [
            ((2026, 5, 25, 12, 30), "수질정화시간"),
            ((2026, 5, 25, 10, 0), "운영중"),
            ((2026, 5, 25, 21, 0), "운영종료"),
            ((2026, 5, 30, 12, 0), "운영중"),
            ((2026, 5, 30, 18, 0), "운영종료"),
            ((2026, 5, 31, 13, 0), "운영중"),
        ],
    )
    def test_status_correct(self, dt_args, expected_status):
        r = _estimate_congestion(_dt(*dt_args))
        assert r["status"] == expected_status

    def test_gender_rates_during_open(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 18, 0))
        assert 0 <= r["male_rate"] <= 100
        assert 0 <= r["female_rate"] <= 100
        assert r["male_rate"] + r["female_rate"] > 0

    def test_gender_rates_zero_during_break(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 12, 30))
        assert r["male_rate"] == 0
        assert r["female_rate"] == 0


# ── Daily trend tests ────────────────────────────────────────────────────────


class TestDailyTrend:
    def test_weekday_trend_length(self):
        t = _daily_trend(_dt(2026, 5, 25, 14, 0))
        assert len(t) == 15

    def test_saturday_trend_length(self):
        t = _daily_trend(_dt(2026, 5, 30, 14, 0))
        assert len(t) == 12

    def test_sunday_trend_length(self):
        t = _daily_trend(_dt(2026, 5, 31, 14, 0))
        assert len(t) == 8

    def test_trend_hours_ascending(self):
        t = _daily_trend(_dt(2026, 5, 25, 14, 0))
        hours = [int(item["hour"].split(":")[0]) for item in t]
        assert hours == list(range(6, 21))

    def test_trend_keys(self):
        t = _daily_trend(_dt(2026, 5, 25, 14, 0))
        item = t[0]
        assert "hour" in item
        assert "level" in item
        assert "label" in item
        assert "color" in item


# ── Hourly forecast tests ────────────────────────────────────────────────────


class TestHourlyForecast:
    def test_returns_operating_hours_only(self):
        f = _hourly_forecast(_dt(2026, 5, 28, 10, 0))
        assert 10 <= len(f) <= 11
        for item in f:
            h = int(item["hour"].split(":")[0])
            assert 6 <= h <= 20

    def test_skips_break_time(self):
        f = _hourly_forecast(_dt(2026, 5, 28, 11, 0))
        hours = [item["hour"] for item in f]
        assert "12:00" not in hours

    def test_starts_at_current_hour(self):
        now = _dt(2026, 5, 28, 10, 30)
        f = _hourly_forecast(now)
        assert f[0]["hour"] == "10:00"
        assert f[1]["hour"] == "11:00"

    def test_weekend_operating_hours(self):
        f = _hourly_forecast(_dt(2026, 5, 30, 8, 0))
        for item in f:
            h = int(item["hour"].split(":")[0])
            assert 6 <= h <= 17
        assert len(f) == 10

    def test_sunday_operating_hours(self):
        f = _hourly_forecast(_dt(2026, 5, 31, 10, 0))
        for item in f:
            h = int(item["hour"].split(":")[0])
            assert 10 <= h <= 17
        assert len(f) == 8

    def test_forecast_keys(self):
        f = _hourly_forecast(_dt(2026, 5, 28, 10, 0))
        item = f[0]
        assert "hour" in item
        assert "level" in item
        assert "label" in item
        assert "color" in item


# ── API response consistency tests ───────────────────────────────────────────


class TestAPIResponseStructure:
    """Test that _estimate_congestion returns all expected fields."""

    REQUIRED_FIELDS = {"level", "label", "color", "tip", "day_type", "is_weekend", "male_rate", "female_rate", "status"}

    def test_all_fields_present_weekday(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 10, 0))
        missing = self.REQUIRED_FIELDS - set(r.keys())
        assert not missing, f"Missing fields: {missing}"

    def test_all_fields_present_weekend(self):
        r = _estimate_congestion(_dt(2026, 5, 30, 10, 0))
        missing = self.REQUIRED_FIELDS - set(r.keys())
        assert not missing, f"Missing fields: {missing}"

    @pytest.mark.parametrize("field", ["level", "label", "color", "tip", "day_type", "is_weekend", "male_rate", "female_rate", "status"])
    def test_field_types(self, field):
        r = _estimate_congestion(_dt(2026, 5, 25, 14, 0))
        val = r[field]
        if field == "level":
            assert isinstance(val, int)
        elif field == "is_weekend":
            assert isinstance(val, bool)
        else:
            assert isinstance(val, str) or isinstance(val, int)

    def test_level_range(self):
        for hour in range(0, 24):
            r = _estimate_congestion(_dt(2026, 5, 25, hour, 0))
            assert 0 <= r["level"] <= 100, f"Level {r['level']} out of range at {hour}:00"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Prediction accuracy tests (forecast vs heuristic consistency)
# ═══════════════════════════════════════════════════════════════════════════════


class TestPredictionConsistency:
    """Tests that predictions produce reasonable, consistent values."""

    def test_get_default_forecast_weekday_levels_reasonable(self):
        """Default forecast should produce levels between 0-95 for all hours."""
        now = _dt(2026, 6, 2, 10, 0)
        result = _get_default_forecast(now)
        assert result is not None
        for hour, level in result.items():
            assert 0 <= level <= 95, f"Hour {hour}: level {level} out of range"

    def test_chart_to_levels_similar_to_heuristic(self, reset_global_cache):
        """Chart predictions should not deviate wildly from heuristic baseline."""
        _main_mod._LIVE_CACHE = {"level": 38}
        chart_data = {6: 88, 7: 29, 8: 42, 9: 22, 10: 40, 13: 39, 14: 16, 15: 22, 16: 20, 17: 17}
        now = _dt(2026, 6, 2, 10, 0)
        preds = _chart_to_levels(chart_data, now)
        assert preds is not None

        for hour, level in preds.items():
            heur = _estimate_congestion(_dt(2026, 6, 2, hour, 0))
            diff = abs(level - heur["level"])
            assert diff <= 40, f"Hour {hour}: prediction {level} vs heuristic {heur['level']} (diff={diff})"

    def test_prediction_priority_chart_over_historical(self, reset_global_cache):
        """When chart predictions exist, they should take priority."""
        _main_mod._CHART_PREDICTIONS.clear()
        _main_mod._CHART_PREDICTIONS.update({10: 42, 11: 30})
        _main_mod._CHART_PREDICTIONS_DATE = "2026-06-02"

        _main_mod._HISTORICAL_PREDICTIONS.clear()
        _main_mod._HISTORICAL_PREDICTIONS.update({10: 80, 11: 70})
        _main_mod._HISTORICAL_DATE = "2026-06-02"

        forecast_entries = [
            {"hour": "10:00", "level": 40, "label": "보통", "color": "#eab308"},
            {"hour": "11:00", "level": 12, "label": "여유", "color": "#22c55e"},
        ]

        # Apply chart predictions (higher priority)
        _apply_levels(forecast_entries, _main_mod._CHART_PREDICTIONS)

        # Should use chart predictions (42, 30) clamped within delta
        assert forecast_entries[0]["level"] == 42  # 40→42 (within delta)
        assert forecast_entries[1]["level"] == 30  # 12→30 (within delta)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — API integration tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAPIEndpoints:
    """FastAPI TestClient integration tests for all endpoints."""

    def test_congestion_endpoint_returns_required_structure(self):
        """GET /api/congestion should return the expected JSON structure."""
        client = TestClient(app)
        response = client.get("/api/congestion")
        assert response.status_code == 200
        data = response.json()
        assert "current" in data
        assert "forecast" in data
        assert "trend" in data
        assert "pool" in data
        assert "time" in data

    def test_congestion_current_has_required_fields(self):
        """Current congestion data should have all expected fields."""
        client = TestClient(app)
        response = client.get("/api/congestion")
        data = response.json()
        current = data["current"]
        required = {"level", "label", "color", "tip", "day_type", "is_weekend",
                     "male_rate", "female_rate", "status", "data_source",
                     "is_closed", "closed_reason"}
        missing = required - set(current.keys())
        assert not missing, f"Missing current fields: {missing}"

    def test_congestion_forecast_is_list(self):
        """Forecast should be a list of hourly items."""
        client = TestClient(app)
        response = client.get("/api/congestion")
        data = response.json()
        assert isinstance(data["forecast"], list)
        if data["forecast"]:
            item = data["forecast"][0]
            assert "hour" in item
            assert "level" in item
            assert "label" in item
            assert "color" in item

    def test_congestion_trend_is_list(self):
        """Trend should be a list of hourly items."""
        client = TestClient(app)
        response = client.get("/api/congestion")
        data = response.json()
        assert isinstance(data["trend"], list)
        if data["trend"]:
            item = data["trend"][0]
            assert "hour" in item
            assert "level" in item
            assert "label" in item
            assert "color" in item

    def test_daily_trend_endpoint(self):
        """GET /api/daily-trend should return trend data."""
        client = TestClient(app)
        response = client.get("/api/daily-trend")
        assert response.status_code == 200
        data = response.json()
        assert "trend" in data
        assert "time" in data

    def test_weekly_schedule_endpoint(self):
        """GET /api/weekly-schedule should return 7-day schedule."""
        client = TestClient(app)
        response = client.get("/api/weekly-schedule")
        assert response.status_code == 200
        data = response.json()
        assert "schedule" in data
        assert len(data["schedule"]) == 7

    def test_weekly_schedule_item_structure(self):
        """Each schedule item should have the expected fields."""
        client = TestClient(app)
        response = client.get("/api/weekly-schedule")
        data = response.json()
        required = {"date", "day_name", "is_today", "is_closed", "closed_reason",
                     "hours", "status", "peak_level", "peak_label", "peak_color"}
        for day in data["schedule"]:
            missing = required - set(day.keys())
            assert not missing, f"Missing schedule fields: {missing}"

    def test_root_endpoint_returns_html(self):
        """GET / should return HTML page."""
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_forecast_levels_in_valid_range(self):
        """All forecast levels should be in 0-100 range."""
        client = TestClient(app)
        response = client.get("/api/congestion")
        data = response.json()
        for f_item in data["forecast"]:
            assert 0 <= f_item["level"] <= 100, f"Forecast level {f_item['level']} out of range"

    def test_trend_levels_in_valid_range(self):
        """All trend levels should be in 0-100 range."""
        client = TestClient(app)
        response = client.get("/api/congestion")
        data = response.json()
        for t_item in data["trend"]:
            assert 0 <= t_item["level"] <= 100, f"Trend level {t_item['level']} out of range"
