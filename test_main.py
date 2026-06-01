"""Tests for the pool congestion estimation logic."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from main import _estimate_congestion, _hourly_forecast, _daily_trend


# ── Helpers ─────────────────────────────────────────────────────────────────


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Create a datetime for testing."""
    return datetime(year, month, day, hour, minute)


# ── Weekday (월~금) tests ────────────────────────────────────────────────────


class TestWeekday:
    """0=Mon … 4=Fri — all use the same weekday schedule."""

    @pytest.mark.parametrize(
        "hour, minute, expected_level, expected_label",
        [
            (0, 0, 10, "여유"),  # before open
            (5, 59, 10, "여유"),  # before open
            (6, 0, 30, "여유"),  # 06:00~08:00
            (7, 0, 30, "여유"),
            (7, 59, 30, "여유"),
            (8, 0, 20, "여유"),  # 08:00~11:00
            (9, 30, 20, "여유"),
            (10, 59, 20, "여유"),
            (11, 0, 40, "여유"),  # 11:00~12:00
            (11, 30, 40, "여유"),
            (11, 59, 40, "여유"),
            (12, 0, 0, "브레이크타임"),  # break
            (12, 30, 0, "브레이크타임"),
            (12, 59, 0, "브레이크타임"),
            (13, 0, 35, "여유"),  # 13:00~15:00
            (14, 0, 35, "여유"),
            (14, 59, 35, "여유"),
            (15, 0, 25, "여유"),  # 15:00~16:00
            (15, 59, 25, "여유"),
            (16, 0, 60, "보통"),  # 16:00~18:00
            (17, 0, 60, "보통"),
            (17, 59, 60, "보통"),
            (18, 0, 80, "혼잡"),  # 18:00~20:00
            (19, 0, 80, "혼잡"),
            (20, 0, 80, "혼잡"),  # closing time included
            (20, 1, 10, "여유"),  # after close
            (21, 0, 10, "여유"),  # after close
            (23, 59, 10, "여유"),
        ],
    )
    def test_all_slots(self, hour, minute, expected_level, expected_label):
        r = _estimate_congestion(_dt(2026, 5, 25, hour, minute))  # Monday
        assert r["level"] == expected_level, f"{hour:02d}:{minute:02d}"
        assert r["label"] == expected_label

    def test_day_type(self):
        for wd in range(5):  # Mon-Fri
            dt = _dt(2026, 5, 25 + wd, 10, 0)
            assert _estimate_congestion(dt)["day_type"] == "평일"

    def test_is_weekend_false(self):
        for wd in range(5):
            dt = _dt(2026, 5, 25 + wd, 10, 0)
            assert _estimate_congestion(dt)["is_weekend"] is False


# ── Saturday tests ───────────────────────────────────────────────────────────


class TestSaturday:
    @pytest.mark.parametrize(
        "hour, minute, expected_level, expected_label",
        [
            (0, 0, 10, "여유"),  # before open
            (5, 59, 10, "여유"),
            (6, 0, 25, "여유"),  # 06:00~08:00
            (7, 0, 25, "여유"),
            (7, 59, 25, "여유"),
            (8, 0, 45, "보통"),  # 08:00~10:00
            (9, 0, 45, "보통"),
            (9, 59, 45, "보통"),
            (10, 0, 65, "보통"),  # 10:00~12:00
            (11, 0, 65, "보통"),
            (11, 59, 65, "보통"),
            (12, 0, 80, "혼잡"),  # 12:00~14:00
            (13, 0, 80, "혼잡"),
            (13, 59, 80, "혼잡"),
            (14, 0, 55, "보통"),  # 14:00~17:00
            (15, 0, 55, "보통"),
            (16, 0, 55, "보통"),
            (17, 0, 55, "보통"),  # closing time included
            (17, 1, 10, "여유"),  # after close
            (18, 0, 10, "여유"),
            (23, 59, 10, "여유"),
        ],
    )
    def test_all_slots(self, hour, minute, expected_level, expected_label):
        r = _estimate_congestion(_dt(2026, 5, 30, hour, minute))  # Saturday
        assert r["level"] == expected_level, f"{hour:02d}:{minute:02d}"
        assert r["label"] == expected_label

    def test_day_type(self):
        dt = _dt(2026, 5, 30, 10, 0)  # Saturday
        assert _estimate_congestion(dt)["day_type"] == "토요일"

    def test_is_weekend_true(self):
        dt = _dt(2026, 5, 30, 10, 0)
        assert _estimate_congestion(dt)["is_weekend"] is True


# ── Sunday tests ─────────────────────────────────────────────────────────────


class TestSunday:
    @pytest.mark.parametrize(
        "hour, minute, expected_level, expected_label",
        [
            (0, 0, 10, "여유"),  # before open
            (9, 0, 10, "여유"),
            (9, 59, 10, "여유"),
            (10, 0, 20, "여유"),  # 10:00~11:00
            (10, 30, 20, "여유"),
            (10, 59, 20, "여유"),
            (11, 0, 55, "보통"),  # 11:00~13:00
            (12, 0, 55, "보통"),
            (12, 59, 55, "보통"),
            (13, 0, 75, "혼잡"),  # 13:00~15:00
            (14, 0, 75, "혼잡"),
            (14, 59, 75, "혼잡"),
            (15, 0, 50, "보통"),  # 15:00~17:00
            (16, 0, 50, "보통"),
            (17, 0, 50, "보통"),  # closing time included
            (17, 1, 10, "여유"),  # after close
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
        r = _estimate_congestion(_dt(2026, 5, 25, 9, 0))  # 20%
        assert r["color"] == "#22c55e"

    def test_yellow_30_to_49(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 13, 30))  # 35%
        assert r["color"] == "#eab308"

    def test_orange_50_to_69(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 16, 30))  # 60%
        assert r["color"] == "#f97316"

    def test_red_70_and_above(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 18, 30))  # 80%
        assert r["color"] == "#ef4444"

    def test_break_time_zero_is_green(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 12, 30))  # 0%
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
        assert r["status"] in ("운영중", "브레이크타임", "운영종료")

    @pytest.mark.parametrize(
        "dt_args, expected_status",
        [
            ((2026, 5, 25, 12, 30), "브레이크타임"),  # weekday break
            ((2026, 5, 25, 10, 0), "운영중"),  # weekday open
            ((2026, 5, 25, 21, 0), "운영종료"),  # weekday closed
            ((2026, 5, 30, 12, 0), "운영중"),  # sat open
            ((2026, 5, 30, 18, 0), "운영종료"),  # sat closed
            ((2026, 5, 31, 13, 0), "운영중"),  # sun open
        ],
    )
    def test_status_correct(self, dt_args, expected_status):
        r = _estimate_congestion(_dt(*dt_args))
        assert r["status"] == expected_status

    def test_gender_rates_during_open(self):
        r = _estimate_congestion(_dt(2026, 5, 25, 18, 0))  # peak
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
        t = _daily_trend(_dt(2026, 5, 25, 14, 0))  # Monday
        assert len(t) == 15  # 06:00~20:00 = 15 hours

    def test_saturday_trend_length(self):
        t = _daily_trend(_dt(2026, 5, 30, 14, 0))  # Saturday
        assert len(t) == 12  # 06:00~17:00 = 12 hours

    def test_sunday_trend_length(self):
        t = _daily_trend(_dt(2026, 5, 31, 14, 0))  # Sunday
        assert len(t) == 8  # 10:00~17:00 = 8 hours

    def test_trend_hours_ascending(self):
        t = _daily_trend(_dt(2026, 5, 25, 14, 0))
        hours = [int(item["hour"].split(":")[0]) for item in t]
        assert hours == list(range(6, 21)), f"Expected 6..20, got {hours}"

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
        # Weekday 10:00 → 10~20 (skip 12 break) = 11 hours
        f = _hourly_forecast(_dt(2026, 5, 28, 10, 0))
        assert 10 <= len(f) <= 11  # 10:00~20:00 minus break
        for item in f:
            h = int(item["hour"].split(":")[0])
            assert 6 <= h <= 20, f"Hour {h} outside operating hours"

    def test_skips_break_time(self):
        f = _hourly_forecast(_dt(2026, 5, 28, 11, 0))
        hours = [item["hour"] for item in f]
        assert "12:00" not in hours, "Break time should not appear in forecast"

    def test_starts_at_current_hour(self):
        now = _dt(2026, 5, 28, 10, 30)
        f = _hourly_forecast(now)
        assert f[0]["hour"] == "10:00"
        assert f[1]["hour"] == "11:00"

    def test_weekend_operating_hours(self):
        # Saturday 08:00 → 08:00~17:00 = 10 hours
        f = _hourly_forecast(_dt(2026, 5, 30, 8, 0))
        for item in f:
            h = int(item["hour"].split(":")[0])
            assert 6 <= h <= 17, f"Sat hour {h} outside operating hours"
        assert len(f) == 10  # 08:00~17:00

    def test_sunday_operating_hours(self):
        # Sunday 10:00 → 10:00~17:00 = 8 hours
        f = _hourly_forecast(_dt(2026, 5, 31, 10, 0))
        for item in f:
            h = int(item["hour"].split(":")[0])
            assert 10 <= h <= 17, f"Sun hour {h} outside operating hours"
        assert len(f) == 8  # 10:00~17:00

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
            assert isinstance(val, int), f"{field} should be int, got {type(val)}"
        elif field == "is_weekend":
            assert isinstance(val, bool), f"{field} should be bool, got {type(val)}"
        else:
            assert isinstance(val, str) or isinstance(val, int), f"{field} should be str/int, got {type(val)}"

    def test_level_range(self):
        for hour in range(0, 24):
            r = _estimate_congestion(_dt(2026, 5, 25, hour, 0))
            assert 0 <= r["level"] <= 100, f"Level {r['level']} out of range at {hour}:00"
