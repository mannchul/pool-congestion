"""전주 라온체육센터 수영장 혼잡도 웹 애플리케이션"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import re as _re
import ssl
import urllib.request

app = FastAPI(
    title="라온체육센터 수영장 혼잡도",
    description="전주 라온체육센터 수영장의 시간대별 예상 혼잡도를 확인하세요.",
)

# ── Pool information ─────────────────────────────────────────────────────────

POOL_INFO = {
    "name": "전주 라온체육센터 수영장",
    "address": "전북 전주시 덕진구 오공로 43-6",
    "phone": "063-239-2760",
    "weekday_hours": "06:00 ~ 20:00 (수질정화시간 12:00~13:00)",
    "saturday_hours": "06:00 ~ 17:00",
    "sunday_holiday_hours": "10:00 ~ 17:00",
    "weekend_hours": "토 06:00~17:00 / 일·공휴일 10:00~17:00",
    "break_time": "12:00~13:00 (평일)",
    "closed_days": "매월 첫째·셋째 주 일요일 / 설날·추석",
    "pool_size": "25m 6레인",
    "features": ["어린이풀", "헬스장", "탁구장"],
}


# ── Holiday / closure detection ────────────────────────────────────────────

# Specific holiday dates (Seollal and Chuseok) - update annually.
# Format: frozenset of (month, day) tuples.
_HOLIDAY_DATES: frozenset = frozenset({
    # 2026
    (2, 17), (2, 18),  # Seollal (Feb 17-18)
    (9, 25), (9, 26),  # Chuseok (Sep 25-26)
    # 2027
    (2, 6), (2, 7),    # Seollal (Feb 6-7)
    (9, 14), (9, 15),  # Chuseok (Sep 14-15)
    # 2028
    (1, 27), (1, 28),  # Seollal (Jan 27-28)
    (10, 3), (10, 4),  # Chuseok (Oct 3-4)
})


def _is_closed_day(now: datetime) -> str | None:
    """Check if today is a closed day.

    Returns a string with the closure reason, or None if open.
    """
    month, day = now.month, now.day
    weekday = now.weekday()  # 0=Mon .. 6=Sun

    # 매월 첫째·셋째 주 일요일 정기휴장
    if weekday == 6:
        if day <= 7:
            return "매월 첫째 주 일요일 정기휴장"
        if 15 <= day <= 21:
            return "매월 셋째 주 일요일 정기휴장"

    # 설날 / 추석
    if (month, day) in _HOLIDAY_DATES:
        return "설날/추석 휴장"

    return None


# ── Real data scraper ──────────────────────────────────────────────────────

_LIVE_CACHE: Dict | None = None
_LIVE_CACHE_TIME: datetime | None = None

# Google Chart hourly usage data cache (extracted from jjss.or.kr page)
_CHART_DATA: dict = {}  # {hour: user_count}
_CHART_PREDICTIONS: dict = {}  # {hour: level}
_CHART_PREDICTIONS_DATE: str | None = None  # "YYYY-MM-DD" of cached predictions

# Historical chart data predictions (from yesterday + last_week history page)
_HISTORICAL_PREDICTIONS: dict = {}  # {hour: level} - improved predictions using history
_HISTORICAL_DATE: str | None = None  # "YYYY-MM-DD" when predictions were built

# Multiple URL patterns for the real-time congestion page
_LIVE_URLS = [
    # Primary URL
    (
        "https://www.jjss.or.kr/reserv/index.9is"
        "?contentUid=5232d76d8f95414801904883b431549b"
        "&searchType=PL004&subPath="
    ),
    # Alternative URL (without subPath)
    (
        "https://www.jjss.or.kr/reserv/index.9is"
        "?contentUid=5232d76d8f95414801904883b431549b"
        "&searchType=PL004"
    ),
]

# History page URL (includes yesterday/lastWeek comparative data)
_HISTORY_URL = (
    "https://www.jjss.or.kr/reserv/index.9is"
    "?contentUid=5232d76d8f95414801904883b431549b"
    "&searchType=PL004&yesterday=yesterday&lastWeek=lastWeek&subPath="
)

_LIVE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


def _try_fetch_url(url: str, timeout: int = 10) -> bytes | None:
    """Try to fetch a URL using urllib with proper headers and SSL handling."""
    try:
        req = urllib.request.Request(url, headers=_LIVE_HEADERS)
        # Approach 1: Normal SSL verification
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if len(data) > 100:
                    return data
        except urllib.error.URLError:
            pass

        # Approach 2: Relaxed SSL (for self-signed certs on Korean gov sites)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()
    except Exception:
        return None


def _scrape_live_data() -> Dict | None:
    """Scrape real-time congestion data from jjss.or.kr.

    Uses urllib for HTTP requests with multiple URL fallbacks.
    Parses HTML using regex patterns.
    Handles EUC-KR encoded pages.

    Returns a dict with 'level', 'male_rate', 'female_rate', 'label',
    'color', and 'scraped_at', or None if all scraping attempts fail.
    """
    global _LIVE_CACHE, _LIVE_CACHE_TIME

    now = datetime.now(ZoneInfo("Asia/Seoul"))

    # Cache for up to 60 seconds
    if _LIVE_CACHE is not None and _LIVE_CACHE_TIME is not None:
        if (now - _LIVE_CACHE_TIME).total_seconds() < 60:
            return _LIVE_CACHE

    raw = None

    # Try each URL
    for url in _LIVE_URLS:
        result = _try_fetch_url(url)
        if result is not None and len(result) > 100:
            raw = result
            break

    if raw is None:
        # Cache the failure briefly to avoid hammering the site
        _LIVE_CACHE = None
        _LIVE_CACHE_TIME = now
        return None

    # Parse with regex on raw bytes
    # Find all percentage patterns: >XX%< in the entire page
    percentages = _re.findall(rb">(\d+)%<", raw)

    if len(percentages) < 1:
        _LIVE_CACHE = None
        _LIVE_CACHE_TIME = now
        return None

    total_pct = int(percentages[0])

    # Male/female rates: 2nd = male (man), 3rd = female (woman) in page order
    male_pct = int(percentages[1]) if len(percentages) > 1 else total_pct // 2
    female_pct = int(percentages[2]) if len(percentages) > 2 else total_pct // 2

    # Determine label from class name (search the whole page)
    # bg_spare = 여유, bg_general = 보통, bg_congestion = 혼잡
    status_label = "여유"
    if _re.search(rb"bg_congestion", raw):
        status_label = "혼잡"
    elif _re.search(rb"bg_general", raw):
        status_label = "보통"
    elif _re.search(rb"bg_spare", raw):
        status_label = "여유"

    # Colour mapping
    if total_pct < 30:
        color = "#22c55e"
    elif total_pct < 50:
        color = "#eab308"
    elif total_pct < 70:
        color = "#f97316"
    else:
        color = "#ef4444"

    result = {
        "level": total_pct,
        "label": status_label,
        "color": color,
        "male_rate": male_pct,
        "female_rate": female_pct,
        "scraped_at": now,
    }

    _LIVE_CACHE = result
    _LIVE_CACHE_TIME = now

    # ── Extract Google Chart hourly usage data ──
    chart_data = _extract_chart_data(raw)
    if chart_data:
        _CHART_DATA.clear()
        _CHART_DATA.update(chart_data)

        # Generate hourly congestion predictions from chart data
        predictions = _chart_to_levels(chart_data, now)
        if predictions:
            _CHART_PREDICTIONS.clear()
            _CHART_PREDICTIONS.update(predictions)
            _CHART_PREDICTIONS_DATE = now.strftime("%Y-%m-%d")

    # ── Fetch history page (yesterday + last_week) ──
    hist_raw = _try_fetch_url(_HISTORY_URL)
    if hist_raw and len(hist_raw) > 100:
        hist_data = _extract_historical_data(hist_raw)
        if hist_data:
            # Build improved predictions using historical patterns
            hist_predictions = _build_historical_predictions(hist_data, now)
            if hist_predictions:
                _HISTORICAL_PREDICTIONS.clear()
                _HISTORICAL_PREDICTIONS.update(hist_predictions)
                _HISTORICAL_DATE = now.strftime("%Y-%m-%d")

    return result


def _extract_chart_data(raw: bytes) -> dict | None:
    """Extract Google Chart hourly user count data from the raw HTML.

    Parses the Google Visualization chart data embedded in the page.
    Returns a dict of {hour: user_count} or None if parsing fails.
    """
    idx = raw.find(b"arrayToDataTable")
    if idx < 0:
        return None
    chunk = raw[idx:idx+2000]
    # Match patterns like: '06...', 88 (hour digits + user count)
    matches = _re.findall(rb"'(\d+)[^']*',\s*(\d+)", chunk)
    if len(matches) < 3:
        return None
    result = {}
    for h, u in matches:
        result[int(h)] = int(u)
    return result if result else None


def _extract_historical_data(raw: bytes) -> dict | None:
    """Extract yesterday and last_week hourly data from history page HTML.

    The history page has a 4-column chart format:
    ['06시', today_val, yesterday_val, last_week_val, style_val]

    Returns {hour: {today, yesterday, last_week}} or None.
    """
    idx = raw.find(b"arrayToDataTable")
    if idx < 0:
        return None
    chunk = raw[idx:idx+2500]
    # Match 4-column pattern: '06시', today, yesterday, last_week
    matches = _re.findall(rb"'(\d+)[^']*',\s*(\d+),\s*(\d+),\s*(\d+)", chunk)
    if len(matches) < 3:
        return None
    result = {}
    for h, today, yesterday, last_week in matches:
        result[int(h)] = {
            "today": int(today),
            "yesterday": int(yesterday),
            "last_week": int(last_week),
        }
    return result if result else None


def _chart_to_levels(chart_data: dict, now: datetime) -> dict | None:
    """Convert hourly user counts to estimated congestion levels.

    Uses a calibration factor derived from the current hour's
    chart value vs actual observed utilization.
    Returns {hour: level} or None.
    """
    current_hour = now.hour
    chart_current = chart_data.get(current_hour)
    if not chart_current or chart_current == 0:
        # Try previous hour as fallback
        for h in range(current_hour - 1, 5, -1):
            if chart_data.get(h, 0) > 0:
                chart_current = chart_data[h]
                break
    if not chart_current or chart_current == 0:
        return None

    # Get current actual utilization from live cache
    if _LIVE_CACHE and _LIVE_CACHE.get("level") is not None:
        current_level = _LIVE_CACHE["level"]
    else:
        return None

    # Calibration factor: current_level / chart_current
    factor = current_level / chart_current

    levels = {}
    for hour, users in chart_data.items():
        est = round(users * factor)
        est = max(0, min(est, 95))  # Clamp 0-95
        levels[hour] = est
    return levels


def _build_historical_predictions(hist_data: dict, now: datetime) -> dict | None:
    """Build congestion predictions using historical (last_week) data.

    Uses the 'last_week' column (same day of week) as the primary
    pattern source, calibrated against today's observed utilization.
    Falls back to 'yesterday' data if last_week is unavailable.

    Returns {hour: level} or None.
    """
    current_hour = now.hour

    # Primary: use last_week (same weekday) data
    last_week_current = hist_data.get(current_hour, {}).get("last_week", 0)
    yesterday_current = hist_data.get(current_hour, {}).get("yesterday", 0)
    today_current = hist_data.get(current_hour, {}).get("today", 0)

    # Determine which data column to use as the pattern
    pattern_source = None
    pattern_key = None

    if last_week_current > 0:
        pattern_source = last_week_current
        pattern_key = "last_week"
    elif yesterday_current > 0:
        pattern_source = yesterday_current
        pattern_key = "yesterday"
    elif today_current > 0:
        pattern_source = today_current
        pattern_key = "today"
    else:
        # Try previous hours as fallback
        for h in range(current_hour - 1, 5, -1):
            for col in ["last_week", "yesterday", "today"]:
                val = hist_data.get(h, {}).get(col, 0)
                if val > 0:
                    pattern_source = val
                    pattern_key = col
                    break
            if pattern_source:
                break

    if not pattern_source:
        return None

    # Get current utilization from live cache
    if not (_LIVE_CACHE and _LIVE_CACHE.get("level") is not None):
        return None

    current_level = _LIVE_CACHE["level"]

    # Calibration factor
    factor = current_level / pattern_source

    levels = {}
    for hour, data in hist_data.items():
        val = data.get(pattern_key, 0)
        if val == 0:
            continue
        est = round(val * factor)
        est = max(0, min(est, 95))
        levels[hour] = est

    return levels if levels else None


# ── Congestion estimation logic ─────────────────────────────────────────────

def _estimate_congestion(now: datetime) -> Dict:
    """Estimate pool congestion based on day-of-week and time-of-day.

    Returns a dict with:
      - level: 0-100 congestion percentage
      - label: descriptive label in Korean
      - color: hex colour for the level
      - tip: helpful tip for the visitor
      - day_type: "평일" / "토요일" / "일요일·공휴일"
      - is_weekend: bool
      - male_rate: estimated male usage rate (%)
      - female_rate: estimated female usage rate (%)
      - status: "운영중" / "수질정화시간" / "운영종료" / "휴장"
    """
    # Check closure first
    closed_reason = _is_closed_day(now)
    if closed_reason:
        return {
            "level": 0,
            "label": "휴장",
            "color": "#ef4444",
            "tip": f"🔒 오늘은 {closed_reason}입니다. 수영장을 이용할 수 없습니다.",
            "day_type": "일요일·공휴일" if now.weekday() == 6 else ("토요일" if now.weekday() == 5 else "평일"),
            "is_weekend": now.weekday() >= 5,
            "male_rate": 0,
            "female_rate": 0,
            "status": "휴장",
        }

    hour = now.hour + now.minute / 60
    weekday = now.weekday()  # 0=Mon … 6=Sun

    if weekday == 6:
        # ── Sunday / Holiday ──
        day_type = "일요일·공휴일"
        if 10 <= hour < 11:
            level, label, tip = 20, "여유", "일요일은 오전 10시에 개장합니다. 개장 직후 한적해요!"
            male_rate, female_rate, status = 18, 22, "운영중"
        elif 11 <= hour < 13:
            level, label, tip = 55, "보통", "일요일 오전, 가족 단위 방문객이 늘기 시작합니다."
            male_rate, female_rate, status = 50, 60, "운영중"
        elif 13 <= hour < 15:
            level, label, tip = 75, "혼잡", "일요일 오후 피크 시간입니다. 방문 시 참고하세요."
            male_rate, female_rate, status = 70, 80, "운영중"
        elif 15 <= hour <= 17:
            level, label, tip = 50, "보통", "일요일 오후 늦게는 비교적 한산해집니다."
            male_rate, female_rate, status = 45, 55, "운영중"
        else:
            level, label, tip = 10, "여유", "현재 운영 시간이 아닙니다. (일요일 10:00~17:00)"
            male_rate, female_rate, status = 5, 5, "운영종료"

    elif weekday == 5:
        # ── Saturday ──
        day_type = "토요일"
        if 6 <= hour < 8:
            level, label, tip = 25, "여유", "토요일 아침, 한적하게 수영하기 좋은 시간입니다!"
            male_rate, female_rate, status = 25, 25, "운영중"
        elif 8 <= hour < 10:
            level, label, tip = 45, "보통", "토요일 오전, 방문객이 조금씩 늘어납니다."
            male_rate, female_rate, status = 40, 50, "운영중"
        elif 10 <= hour < 12:
            level, label, tip = 65, "보통", "토요일 오전 피크 시간입니다."
            male_rate, female_rate, status = 60, 70, "운영중"
        elif 12 <= hour < 14:
            level, label, tip = 80, "혼잡", "토요일 점심~오후 가장 붐빕니다."
            male_rate, female_rate, status = 75, 85, "운영중"
        elif 14 <= hour <= 17:
            level, label, tip = 55, "보통", "토요일 오후, 오전보다는 한산합니다."
            male_rate, female_rate, status = 50, 60, "운영중"
        else:
            level, label, tip = 10, "여유", "현재 운영 시간이 아닙니다. (토요일 06:00~17:00)"
            male_rate, female_rate, status = 5, 5, "운영종료"

    else:
        # ── Weekday ──
        # Based on analysis of actual jjss.or.kr chart data:
        # Morning has the most users (06~08시 peak), then steadily drops
        # Afternoon is consistently quiet (10~20% range)
        day_type = "평일"
        if 6 <= hour < 8:
            level, label, tip = 35, "여유", "아침 시간대, 비교적 한적하게 수영할 수 있습니다."
            male_rate, female_rate, status = 35, 35, "운영중"
        elif 8 <= hour < 10:
            level, label, tip = 25, "여유", "오전 시간, 방문객이 점차 줄어듭니다."
            male_rate, female_rate, status = 25, 25, "운영중"
        elif 10 <= hour < 11:
            level, label, tip = 15, "여유", "오전 늦게, 한적한 시간대입니다."
            male_rate, female_rate, status = 15, 15, "운영중"
        elif 11 <= hour < 12:
            level, label, tip = 12, "여유", "점심 전 가장 한적한 시간입니다."
            male_rate, female_rate, status = 10, 14, "운영중"
        elif 12 <= hour < 13:
            level, label, tip = 0, "수질정화시간", "⚠️ 수질정화시간(12:00~13:00)입니다. 시설 정비 시간이니 참고하세요."
            male_rate, female_rate, status = 0, 0, "수질정화시간"
        elif 13 <= hour < 14:
            level, label, tip = 30, "여유", "점심 이후, 방문객이 다시 증가합니다."
            male_rate, female_rate, status = 30, 30, "운영중"
        elif 14 <= hour < 15:
            level, label, tip = 22, "여유", "오후 시간대, 비교적 여유롭습니다."
            male_rate, female_rate, status = 20, 24, "운영중"
        elif 15 <= hour < 16:
            level, label, tip = 18, "여유", "오후 늦게, 한적하게 이용할 수 있습니다."
            male_rate, female_rate, status = 16, 20, "운영중"
        elif 16 <= hour < 18:
            level, label, tip = 18, "여유", "오후 늦은 시간, 평일 중 가장 여유로운 시간대입니다."
            male_rate, female_rate, status = 16, 20, "운영중"
        elif 18 <= hour <= 20:
            level, label, tip = 15, "여유", "저녁 시간, 가벼운 운동하기 좋습니다."
            male_rate, female_rate, status = 14, 16, "운영중"
        else:
            level, label, tip = 10, "여유", "현재 운영 시간이 아닙니다. (평일 06:00~20:00)"
            male_rate, female_rate, status = 5, 5, "운영종료"

    # Colour mapping
    if level < 30:
        color = "#22c55e"  # green
    elif level < 50:
        color = "#eab308"  # yellow
    elif level < 70:
        color = "#f97316"  # orange
    else:
        color = "#ef4444"  # red

    return {
        "level": level,
        "label": label,
        "color": color,
        "tip": tip,
        "day_type": day_type,
        "is_weekend": weekday >= 5,
        "male_rate": male_rate,
        "female_rate": female_rate,
        "status": status,
    }


def _now_kst() -> datetime:
    """Return current time in KST (Asia/Seoul timezone)."""
    return datetime.now(ZoneInfo("Asia/Seoul"))


def _weekly_schedule(now: datetime) -> List[Dict]:
    """Generate a 7-day operation schedule starting from today.

    Returns a list of day dicts with:
      - date: "M.D" formatted date string
      - day_name: Korean day name (e.g. "월", "화")
      - is_today: bool
      - is_closed: bool
      - closed_reason: str or None
      - hours: operating hours string (e.g. "06:00~20:00")
      - status: "운영" / "휴장" / "단축"
      - peak_level: estimated peak congestion level (0-100)
      - peak_label: peak congestion label
      - peak_color: hex color for peak
    """
    DAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
    schedule = []

    for offset in range(7):
        day = now + timedelta(days=offset)
        day = day.replace(hour=12, minute=0, second=0, microsecond=0)  # noon reference

        closed_reason = _is_closed_day(day)
        weekday = day.weekday()

        # Determine operating hours string
        if closed_reason:
            hours_str = "--:--~--:--"
            status = "휴장"
            peak_level = 0
            peak_label = "휴장"
            peak_color = "#ef4444"
        else:
            start_h, end_h = _get_operating_hours(day)
            hours_str = f"{start_h:02d}:00~{end_h:02d}:00"
            status = "운영"

            # Estimate peak congestion level for this day
            # Check each operating hour to find peak
            peak_level = 0
            peak_label = "여유"
            peak_color = "#22c55e"
            for h in range(start_h, end_h + 1):
                check = day.replace(hour=h)
                data = _estimate_congestion(check)
                if data["status"] == "수질정화시간":
                    continue
                if data["level"] > peak_level:
                    peak_level = data["level"]
                    peak_label = data["label"]
                    peak_color = data["color"]

        schedule.append({
            "date": f"{day.month}.{day.day}",
            "day_name": DAY_KR[weekday],
            "is_today": offset == 0,
            "is_closed": closed_reason is not None,
            "closed_reason": closed_reason,
            "hours": hours_str,
            "status": status,
            "peak_level": peak_level,
            "peak_label": peak_label,
            "peak_color": peak_color,
        })

    return schedule


def _get_operating_hours(now: datetime) -> tuple[int, int]:
    """Return (start_hour, end_hour) for the given day based on operating hours.

    Returns (0, 0) if the day is closed.
    """
    if _is_closed_day(now):
        return (0, 0)

    weekday = now.weekday()
    if weekday == 6:
        return (10, 17)  # Sunday: 10:00~17:00
    elif weekday == 5:
        return (6, 17)   # Saturday: 06:00~17:00
    else:
        return (6, 20)   # Weekday: 06:00~20:00


def _hourly_forecast(now: datetime) -> List[Dict]:
    """Generate congestion forecast for operating hours only."""
    if _is_closed_day(now):
        return []
    start_hour, end_hour = _get_operating_hours(now)
    base = now.replace(minute=0, second=0, microsecond=0)
    forecasts = []
    for offset in range(0, 13):
        t = base + timedelta(hours=offset)
        # Only include hours within operating hours
        if t.hour < start_hour or t.hour > end_hour:
            continue
        # Exclude break time label from forecast
        data = _estimate_congestion(t)
        if data["status"] == "수질정화시간":
            continue
        forecasts.append({
            "hour": t.strftime("%H:%M"),
            "level": data["level"],
            "label": data["label"],
            "color": data["color"],
        })
    return forecasts


def _daily_trend(now: datetime) -> List[Dict]:
    """Generate congestion trend data for the full operating day."""
    if _is_closed_day(now):
        return []

    weekday = now.weekday()

    if weekday == 6:
        # Sunday: 10:00~17:00
        start_hour, end_hour = 10, 17
    elif weekday == 5:
        # Saturday: 06:00~17:00
        start_hour, end_hour = 6, 17
    else:
        # Weekday: 06:00~20:00
        start_hour, end_hour = 6, 20

    trend = []
    base = now.replace(minute=0, second=0, microsecond=0)
    t = base.replace(hour=start_hour, minute=0)
    while t.hour <= end_hour:
        data = _estimate_congestion(t)
        trend.append({
            "hour": t.strftime("%H:%M"),
            "level": data["level"],
            "label": data["label"],
            "color": data["color"],
        })
        t += timedelta(hours=1)
    return trend


# ── API endpoints ───────────────────────────────────────────────────────────

@app.get("/api/congestion")
async def get_congestion():
    """Return current congestion data as JSON.

    Uses live data from jjss.or.kr when available, falls back to heuristics.
    """
    now = _now_kst()

    # Check closure first (live data isn't relevant on closed days)
    closed_reason = _is_closed_day(now)
    is_closed = closed_reason is not None

    # Try to get live data (skip if closed)
    live = _scrape_live_data() if not is_closed else None

    if live is not None:
        # Use live data for current, but get day_type/is_weekend from heuristic
        heur = _estimate_congestion(now)
        current = {
            "level": live["level"],
            "label": live["label"],
            "color": live["color"],
            "tip": heur["tip"],
            "day_type": heur["day_type"],
            "is_weekend": heur["is_weekend"],
            "male_rate": live["male_rate"],
            "female_rate": live["female_rate"],
            "status": heur["status"],
            "data_source": "live",
        }
    else:
        current = _estimate_congestion(now)
        current["data_source"] = "heuristic"

    current["is_closed"] = is_closed
    current["closed_reason"] = closed_reason
    current["last_updated"] = live["scraped_at"].strftime("%Y-%m-%d %H:%M:%S") if live is not None else None

    forecast = _hourly_forecast(now)
    trend = _daily_trend(now)

    # ── Helper: apply predictions dict to override forecast/trend items ──
    def _apply_levels(items: list, predictions: dict) -> None:
        for item in items:
            h = int(item["hour"].split(":")[0])
            level = predictions.get(h)
            if level is not None:
                item["level"] = level
                if level < 30:
                    item["label"] = "여유"
                    item["color"] = "#22c55e"
                elif level < 50:
                    item["label"] = "보통"
                    item["color"] = "#eab308"
                elif level < 70:
                    item["label"] = "혼잡"
                    item["color"] = "#f97316"
                else:
                    item["label"] = "매우혼잡"
                    item["color"] = "#ef4444"

    # ── Override with historical-based predictions (yesterday/last_week) ──
    _hist_cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    if _HISTORICAL_PREDICTIONS and _HISTORICAL_DATE and _HISTORICAL_DATE >= _hist_cutoff:
        _apply_levels(forecast, _HISTORICAL_PREDICTIONS)
        _apply_levels(trend, _HISTORICAL_PREDICTIONS)
    elif _CHART_PREDICTIONS and _CHART_PREDICTIONS_DATE == now.strftime("%Y-%m-%d"):
        _apply_levels(forecast, _CHART_PREDICTIONS)
        _apply_levels(trend, _CHART_PREDICTIONS)

    return {
        "current": current,
        "forecast": forecast,
        "trend": trend,
        "pool": POOL_INFO,
        "time": now.strftime("%Y-%m-%d %H:%M"),
    }


@app.get("/api/daily-trend")
async def get_daily_trend():
    """Return full-day congestion trend data as JSON."""
    now = _now_kst()
    return {
        "trend": _daily_trend(now),
        "time": now.strftime("%Y-%m-%d %H:%M"),
    }


@app.get("/api/weekly-schedule")
async def get_weekly_schedule():
    """Return the 7-day operation schedule as JSON."""
    now = _now_kst()
    return {
        "schedule": _weekly_schedule(now),
        "time": now.strftime("%Y-%m-%d %H:%M"),
    }


# ── Web page ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """Render the main congestion dashboard page."""
    return HTMLResponse(HTML_PAGE)


HTML_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>라온체육센터 수영장 혼잡도</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E🏊%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700;900&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg-deep: #040a18;
    --bg-mid: #081226;
    --bg-surface: #0c1830;
    --card: rgba(12, 24, 48, 0.7);
    --card-border: rgba(56, 189, 248, 0.06);
    --card-hover: rgba(20, 40, 70, 0.85);
    --card-glow: rgba(56, 189, 248, 0.04);
    --text: #e2e8f0;
    --text-dim: #7e93b0;
    --text-bright: #f8fafc;
    --accent: #38bdf8;
    --accent-dim: #1d7aa8;
    --accent-glow: rgba(56, 189, 248, 0.25);
    --ocean-dark: #0a3d6b;
    --ocean-mid: #0e5a8a;
    --ocean-light: #1a7fc4;
    --cyan-glow: rgba(6, 182, 212, 0.15);
    --radius: 24px;
    --radius-sm: 14px;
    --radius-xs: 10px;
    --transition: 0.4s cubic-bezier(0.22, 1, 0.36, 1);
    --bounce: 0.5s cubic-bezier(0.34, 1.56, 0.64, 1);
  }

  html { scroll-behavior: smooth; }

  /* Safe area for notched phones */
  @supports(padding:max(0px)) {
    .container { padding-left: max(24px, env(safe-area-inset-left)); padding-right: max(24px, env(safe-area-inset-right)); }
  }

  /* Prevent overscroll on forecast horizontal scroll */
  .forecast-grid { overscroll-behavior: contain; }

  body {
    font-family: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--bg-deep);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.6;
    overflow-x: hidden;
    position: relative;
  }

  /* ── Animated ocean background ──────────────────────────── */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    z-index: -1;
    background:
      radial-gradient(ellipse 90% 65% at 50% -15%, rgba(56, 189, 248, 0.07) 0%, transparent 100%),
      radial-gradient(ellipse 70% 55% at 80% 90%, rgba(6, 182, 212, 0.04) 0%, transparent 100%),
      radial-gradient(ellipse 60% 45% at 20% 95%, rgba(99, 102, 241, 0.03) 0%, transparent 100%),
      linear-gradient(180deg, var(--bg-deep) 0%, var(--bg-mid) 40%, var(--bg-surface) 100%);
    animation: bg-shift 15s ease-in-out infinite alternate;
  }

  @keyframes bg-shift {
    0% { background-position: 0% 0%; }
    50% { background-position: 3% 2%; }
    100% { background-position: -2% 1%; }
  }

  /* ── Animated wave overlay ──────────────────────────────── */
  .wave-overlay {
    position: fixed;
    inset: 0;
    z-index: -1;
    pointer-events: none;
    overflow: hidden;
  }
  .wave-overlay svg {
    position: absolute;
    bottom: 0;
    width: 200%;
    height: 120px;
    opacity: 0.03;
    animation: wave-drift 12s linear infinite;
  }
  .wave-overlay svg:nth-child(2) {
    bottom: 30px;
    opacity: 0.02;
    animation-duration: 16s;
    animation-direction: reverse;
  }
  @keyframes wave-drift {
    0% { transform: translateX(0); }
    100% { transform: translateX(-50%); }
  }

  /* ── Floating orbs ──────────────────────────────────────── */
  .orb {
    position: fixed;
    border-radius: 50%;
    filter: blur(100px);
    z-index: -1;
    pointer-events: none;
    animation: orb-float 20s ease-in-out infinite alternate;
  }
  .orb:nth-child(1) {
    width: 600px; height: 600px;
    background: var(--accent);
    top: -200px; left: -150px;
    opacity: 0.06;
    animation-duration: 18s;
  }
  .orb:nth-child(2) {
    width: 450px; height: 450px;
    background: #22d3ee;
    bottom: -120px; right: -100px;
    opacity: 0.04;
    animation-duration: 22s;
    animation-delay: -6s;
  }
  .orb:nth-child(3) {
    width: 350px; height: 350px;
    background: #6366f1;
    top: 45%; left: 55%;
    opacity: 0.03;
    animation-duration: 25s;
    animation-delay: -12s;
  }

  @keyframes orb-float {
    0% { transform: translate(0, 0) scale(1); }
    25% { transform: translate(40px, -30px) scale(1.1); }
    50% { transform: translate(-30px, 20px) scale(0.9); }
    75% { transform: translate(20px, -15px) scale(1.05); }
    100% { transform: translate(-15px, 25px) scale(0.95); }
  }

  /* ── Subtle noise texture ───────────────────────────────── */
  body::after {
    content: '';
    position: fixed;
    inset: 0;
    z-index: -1;
    opacity: 0.015;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
    background-repeat: repeat;
    background-size: 256px 256px;
    pointer-events: none;
  }

  /* ── Custom scrollbar ──────────────────────────────────── */
  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb {
    background: rgba(56, 189, 248, 0.15);
    border-radius: 10px;
  }
  ::-webkit-scrollbar-thumb:hover { background: rgba(56, 189, 248, 0.3); }

  /* ── Layout ──────────────────────────────────────────── */
  .container {
    max-width: 960px;
    margin: 0 auto;
    padding: 24px 24px 60px;
    position: relative;
  }

  /* ── Animations ────────────────────────────────────────── */
  @keyframes fade-in-up {
    from { opacity: 0; transform: translateY(24px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }
  @keyframes scale-in {
    from { opacity: 0; transform: scale(0.8); }
    to { opacity: 1; transform: scale(1); }
  }
  @keyframes slide-in-right {
    from { opacity: 0; transform: translateX(20px); }
    to { opacity: 1; transform: translateX(0); }
  }
  @keyframes pulse-glow {
    0%, 100% { box-shadow: 0 0 8px var(--gauge-glow, transparent); }
    50% { box-shadow: 0 0 20px var(--gauge-glow, transparent); }
  }

  .animate-in {
    animation: fade-in-up 0.7s var(--transition) both;
  }
  .animate-in-delay-1 { animation-delay: 0.08s; }
  .animate-in-delay-2 { animation-delay: 0.16s; }
  .animate-in-delay-3 { animation-delay: 0.24s; }
  .animate-in-delay-4 { animation-delay: 0.32s; }
  .animate-in-delay-5 { animation-delay: 0.40s; }

  /* ── Header ──────────────────────────────────────────── */
  header {
    text-align: center;
    padding: 32px 0 10px;
    position: relative;
  }
  header::after {
    content: '';
    display: block;
    width: 80px;
    height: 3px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    border-radius: 4px;
    margin: 16px auto 0;
    opacity: 0.5;
  }
  header h1 {
    font-size: 1.85rem;
    font-weight: 900;
    color: var(--text-bright);
    letter-spacing: -0.8px;
    line-height: 1.3;
  }
  header h1 .pool-emoji {
    display: inline-block;
    animation: pool-bob 3.5s ease-in-out infinite;
    filter: drop-shadow(0 0 6px rgba(56, 189, 248, 0.2));
  }
  @keyframes pool-bob {
    0%, 100% { transform: translateY(0) rotate(0deg); }
    25% { transform: translateY(-5px) rotate(-4deg); }
    75% { transform: translateY(-2px) rotate(4deg); }
  }
  header h1 .accent-text {
    color: var(--accent);
    text-shadow: 0 0 30px rgba(56, 189, 248, 0.15);
  }
  header p {
    color: var(--text-dim);
    font-size: 0.85rem;
    margin-top: 6px;
    letter-spacing: 0.3px;
  }

  /* ── Time bar ──────────────────────────────────────────── */
  .time-bar {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 16px;
    margin: 22px auto 32px;
    padding: 14px 28px;
    background: rgba(12, 24, 48, 0.4);
    border: 1px solid rgba(56, 189, 248, 0.06);
    border-radius: 50px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    width: fit-content;
  }
  .time-bar .live-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #22c55e;
    box-shadow: 0 0 10px rgba(34, 197, 94, 0.5);
    animation: pulse-dot 1.8s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes pulse-dot {
    0%, 100% { opacity: 1; transform: scale(1); box-shadow: 0 0 10px rgba(34, 197, 94, 0.5); }
    50% { opacity: 0.4; transform: scale(1.5); box-shadow: 0 0 20px rgba(34, 197, 94, 0.2); }
  }
  #clock-display {
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.5px;
    font-weight: 600;
    font-size: 1.1rem;
    color: var(--text-bright);
  }
  #date-display {
    font-size: 0.8rem;
    color: rgba(126, 147, 176, 0.7);
    font-weight: 400;
    letter-spacing: 0.2px;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Status badge ──────────────────────────────────── */
  .gauge-status-badge {
    position: absolute;
    top: 18px;
    right: 22px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 6px 16px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.3px;
    z-index: 2;
    transition: all 0.4s;
  }
  .gauge-status-badge .status-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    animation: pulse-dot 1.5s ease-in-out infinite;
  }
  .gauge-status-badge.open {
    background: rgba(34, 197, 94, 0.1);
    color: #4ade80;
    border: 1px solid rgba(34, 197, 94, 0.15);
  }
  .gauge-status-badge.open .status-dot { background: #4ade80; }
  .gauge-status-badge.break {
    background: rgba(250, 204, 21, 0.1);
    color: #facc15;
    border: 1px solid rgba(250, 204, 21, 0.15);
  }
  .gauge-status-badge.break .status-dot { background: #facc15; animation: pulse-dot 0.6s ease-in-out infinite; }
  .gauge-status-badge.closed {
    background: rgba(148, 163, 184, 0.08);
    color: #94a3b8;
    border: 1px solid rgba(148, 163, 184, 0.1);
  }
  .gauge-status-badge.closed .status-dot { background: #94a3b8; animation: none; }
  .gauge-status-badge.closed-day {
    background: rgba(239, 68, 68, 0.1);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.15);
  }
  .gauge-status-badge.closed-day .status-dot { background: #f87171; animation: none; }

  /* ── Closure banner ─────────────────────────────── */
  .closed-banner {
    display: none;
    margin-bottom: 22px;
    padding: 18px 22px;
    border-radius: var(--radius-sm);
    background: linear-gradient(135deg, rgba(239, 68, 68, 0.08), rgba(248, 113, 113, 0.04));
    border: 1px solid rgba(239, 68, 68, 0.12);
    text-align: center;
    animation: fade-in-up 0.6s var(--transition) both;
  }
  .closed-banner .cb-icon {
    font-size: 2.2rem;
    display: block;
    margin-bottom: 8px;
  }
  .closed-banner .cb-title {
    font-size: 1.15rem;
    font-weight: 800;
    color: #f87171;
    margin-bottom: 4px;
    letter-spacing: -0.3px;
  }
  .closed-banner .cb-desc {
    font-size: 0.85rem;
    color: rgba(248, 113, 113, 0.7);
    line-height: 1.5;
  }
  .closed-banner .cb-closure {
    margin-top: 10px;
    padding: 8px 18px;
    display: inline-block;
    border-radius: 20px;
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.1);
    font-size: 0.78rem;
    color: #f87171;
    font-weight: 600;
    letter-spacing: 0.2px;
  }

  /* ── Gender rates ───────────────────────────────────── */
  .gender-rates {
    margin: 24px auto 0;
    max-width: 360px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 20px 24px;
    border-radius: var(--radius-sm);
    background: rgba(0, 0, 0, 0.2);
    border: 1px solid rgba(148, 163, 184, 0.04);
  }
  .gender-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .gender-icon {
    width: 24px;
    text-align: center;
    font-size: 1.1rem;
    font-weight: 700;
  }
  .gender-label {
    font-size: 0.78rem;
    color: var(--text-dim);
    width: 40px;
    font-weight: 500;
  }
  .gender-bar-track {
    flex: 1;
    height: 12px;
    background: rgba(148, 163, 184, 0.06);
    border-radius: 10px;
    overflow: hidden;
    position: relative;
  }
  .gender-bar-track::before {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: 10px;
    box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.2);
    pointer-events: none;
  }
  .gender-bar-fill {
    height: 100%;
    border-radius: 10px;
    transition: width 1.2s var(--bounce);
    position: relative;
  }
  .gender-bar-fill::after {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: 10px;
    background: linear-gradient(to bottom, rgba(255,255,255,0.15) 0%, transparent 60%);
    pointer-events: none;
  }
  .gender-bar-fill.male { background: linear-gradient(90deg, #3b82f6, #60a5fa); }
  .gender-bar-fill.female { background: linear-gradient(90deg, #ec4899, #f472b6); }
  .gender-value {
    font-size: 0.85rem;
    font-weight: 700;
    width: 42px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .gender-row:first-child .gender-value { color: #60a5fa; }
  .gender-row:last-child .gender-value { color: #f472b6; }

  /* ── Section title ──────────────────────────────────── */
  .section-title {
    font-size: 1.05rem;
    font-weight: 700;
    color: var(--text-bright);
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    gap: 10px;
    letter-spacing: -0.3px;
  }
  .section-title .badge-count {
    font-size: 0.68rem;
    font-weight: 600;
    background: rgba(56, 189, 248, 0.08);
    padding: 3px 12px;
    border-radius: 20px;
    color: var(--accent);
    margin-left: auto;
    border: 1px solid rgba(56, 189, 248, 0.1);
  }

  /* ── Glass card base ──────────────────────────────────── */
  .glass-card {
    background: var(--card);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border: 1px solid var(--card-border);
    border-radius: var(--radius);
    transition: transform var(--transition), box-shadow var(--transition), background var(--transition), border-color var(--transition);
    position: relative;
    overflow: hidden;
  }
  .glass-card::before {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: radial-gradient(circle at 30% 0%, var(--card-glow) 0%, transparent 70%);
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.6s;
  }
  .glass-card:hover {
    transform: translateY(-4px);
    box-shadow: 0 12px 48px rgba(0, 0, 0, 0.3), 0 0 0 1px rgba(56, 189, 248, 0.04);
    background: var(--card-hover);
    border-color: rgba(56, 189, 248, 0.1);
  }
  .glass-card:hover::before { opacity: 1; }

  /* ── Gauge card ──────────────────────────────────────── */
  .gauge-card {
    padding: 44px 32px 36px;
    text-align: center;
    position: relative;
    overflow: hidden;
  }
  .gauge-card::after {
    content: '';
    position: absolute;
    top: -50%;
    left: -50%;
    width: 200%;
    height: 200%;
    background: conic-gradient(from 0deg, transparent, var(--accent-glow), transparent, var(--accent-glow), transparent);
    opacity: 0.02;
    animation: gauge-shimmer 8s linear infinite;
    pointer-events: none;
  }
  @keyframes gauge-shimmer {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
  }

  /* ── Data source badge ──────────────────────────────── */
  .source-badge {
    position: absolute;
    bottom: 18px;
    left: 24px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 0.63rem;
    font-weight: 500;
    letter-spacing: 0.2px;
    z-index: 2;
  }
  .source-badge.live {
    background: rgba(56, 189, 248, 0.08);
    color: #38bdf8;
    border: 1px solid rgba(56, 189, 248, 0.1);
  }
  .source-badge.live::before {
    content: '';
    display: inline-block;
    width: 5px; height: 5px;
    border-radius: 50%;
    background: #38bdf8;
    animation: pulse-dot 1.5s ease-in-out infinite;
  }
  .source-badge.heuristic {
    background: rgba(148, 163, 184, 0.06);
    color: #94a3b8;
    border: 1px solid rgba(148, 163, 184, 0.06);
  }
  .source-badge.heuristic::before {
    content: '📊';
    font-size: 0.55rem;
  }

  /* ── Last updated timestamp ────────────────────────── */
  .last-updated {
    position: absolute;
    bottom: 18px;
    right: 24px;
    font-size: 0.58rem;
    color: rgba(126, 147, 176, 0.4);
    font-weight: 400;
    letter-spacing: 0.2px;
    z-index: 2;
  }
  .last-updated.live {
    color: rgba(56, 189, 248, 0.5);
  }

  .gauge-card::before {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: radial-gradient(circle at 50% 0%, rgba(56, 189, 248, 0.05) 0%, transparent 70%);
    pointer-events: none;
  }

  /* ── Gauge ring ──────────────────────────────────────── */
  .gauge-ring {
    width: 200px; height: 200px;
    margin: 0 auto 16px;
    position: relative;
  }
  .gauge-ring svg {
    transform: rotate(-90deg);
    filter: drop-shadow(0 0 8px var(--gauge-glow, transparent));
    transition: filter 0.8s;
  }
  .gauge-ring .bg-circle {
    fill: none;
    stroke: rgba(30, 50, 80, 0.5);
    stroke-width: 10;
  }
  .gauge-ring .fg-circle {
    fill: none;
    stroke-width: 10;
    stroke-linecap: round;
    transition: stroke-dashoffset 1.4s var(--bounce), stroke 0.6s;
  }

  .gauge-label {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .gauge-label .pct {
    font-size: 2.9rem;
    font-weight: 900;
    line-height: 1;
    transition: color 0.6s;
    letter-spacing: -1px;
  }
  .gauge-label .pct-label {
    font-size: 1.05rem;
    font-weight: 600;
    margin-top: 6px;
    transition: color 0.6s;
    letter-spacing: 0.5px;
    opacity: 0.9;
  }

  /* ── Tip ──────────────────────────────────────────────── */
  .gauge-tip {
    margin-top: 22px;
    padding: 18px 24px;
    background: rgba(56, 189, 248, 0.04);
    border-radius: var(--radius-sm);
    font-size: 0.88rem;
    color: var(--text-dim);
    border-left: 3px solid var(--accent);
    text-align: left;
    transition: border-color 0.6s, background 0.6s;
    line-height: 1.6;
    position: relative;
  }
  .gauge-tip::before {
    content: '💡';
    margin-right: 6px;
  }

  /* ── Info cards ───────────────────────────────────────── */
  .info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 16px;
    margin: 32px 0;
  }
  .info-item {
    background: var(--card);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    border-radius: var(--radius-sm);
    padding: 20px 24px;
    transition: transform var(--transition), background var(--transition), box-shadow var(--transition);
    position: relative;
    overflow: hidden;
  }
  .info-item::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 2px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    opacity: 0;
    transition: opacity 0.4s;
  }
  .info-item:hover::before { opacity: 0.5; }
  .info-item:hover {
    background: var(--card-hover);
    transform: translateY(-3px);
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.2);
  }
  .info-item .label {
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    margin-bottom: 6px;
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .info-item .value {
    font-size: 0.92rem;
    font-weight: 500;
    color: var(--text-bright);
    line-height: 1.4;
  }
  .info-item .value.highlight { color: var(--accent); }

  /* ── Combined hours card (compact) ──────────────────── */
  .hours-combined {
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 12px;
    padding: 12px 18px;
  }
  .hours-row {
    flex: 1;
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 2px;
  }
  .hours-label {
    font-size: 0.62rem;
    font-weight: 600;
    color: var(--text-dim);
    letter-spacing: 0.3px;
    white-space: nowrap;
  }
  .hours-value {
    font-size: 0.72rem;
    font-weight: 500;
    color: var(--text-bright);
    text-align: left;
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .hours-divider {
    width: 1px;
    height: 20px;
    background: linear-gradient(to bottom, transparent, rgba(56, 189, 248, 0.15), transparent);
    flex-shrink: 0;
  }

  /* ── Day-type badge ───────────────────────────────────── */
  .day-badge {
    display: inline-block;
    padding: 5px 16px;
    border-radius: 20px;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.3px;
    transition: all 0.3s;
  }
  .day-badge.weekday {
    background: rgba(34, 197, 94, 0.08);
    color: #4ade80;
    border: 1px solid rgba(34, 197, 94, 0.12);
  }
  .day-badge.weekend {
    background: rgba(239, 68, 68, 0.08);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.12);
  }
  /* ── Forecast ─────────────────────────────────────────── */
  .forecast-section { margin-top: 36px; }

  .forecast-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
    gap: 8px;
  }
  .forecast-item {
    background: var(--card);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    border-radius: var(--radius-xs);
    padding: 10px 4px 10px;
    text-align: center;
    transition: transform var(--transition), background var(--transition), box-shadow var(--transition), border-color var(--transition);
    cursor: default;
    animation: scale-in 0.4s var(--bounce) both;
    flex-shrink: 0;
    position: relative;
    overflow: hidden;
  }
  .forecast-item::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 1px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    opacity: 0;
    transition: opacity 0.3s;
  }
  .forecast-item:hover {
    background: var(--card-hover);
    transform: translateY(-3px) scale(1.02);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
    border-color: rgba(56, 189, 248, 0.08);
  }
  .forecast-item:hover::before { opacity: 0.3; }
  .forecast-item .hour {
    font-size: 0.7rem;
    color: var(--text-dim);
    margin-bottom: 6px;
    font-weight: 500;
    letter-spacing: 0.2px;
  }
  .forecast-item .bar-wrap {
    height: 48px;
    display: flex;
    align-items: flex-end;
    justify-content: center;
    margin-bottom: 6px;
    overflow: hidden;
  }
  .forecast-item .bar {
    width: 18px;
    border-radius: 4px 4px 2px 2px;
    min-height: 2px;
    transition: height 0.9s var(--bounce), background 0.3s;
    position: relative;
  }
  .forecast-item .bar::after {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: linear-gradient(to top, transparent 30%, rgba(255,255,255,0.2) 100%);
    pointer-events: none;
  }
  .forecast-item .bar::before {
    content: '';
    position: absolute;
    top: -1px; left: -2px;
    right: -2px; height: 4px;
    border-radius: 3px;
    background: inherit;
    filter: blur(3px);
    opacity: 0.5;
    pointer-events: none;
  }
  .forecast-item .f-label {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: -0.1px;
  }

  /* ── Weekly schedule (calendar design) ──────────────── */
  .weekly-section { margin: 28px 0; }
  .weekly-card {
    padding: 24px 22px 20px;
    display: flex;
    flex-direction: column;
    gap: 0;
  }
  .weekly-card::before {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: radial-gradient(circle at 30% 0%, var(--card-glow) 0%, transparent 70%);
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.6s;
  }
  .weekly-card:hover::before { opacity: 1; }

  /* Calendar row: 7 day cells side by side */
  .weekly-strip {
    display: flex;
    gap: 10px;
    justify-content: center;
  }

  /* Individual day cell — like a calendar page */
  .weekly-col {
    flex: 1;
    max-width: 110px;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 14px 6px 12px;
    background: rgba(0, 0, 0, 0.15);
    border: 1px solid rgba(56, 189, 248, 0.04);
    border-radius: var(--radius-xs);
    position: relative;
    transition: background 0.3s, border-color 0.3s, transform 0.3s;
    cursor: default;
    animation: scale-in 0.5s var(--bounce) both;
  }
  .weekly-col:hover {
    background: rgba(56, 189, 248, 0.04);
    border-color: rgba(56, 189, 248, 0.08);
    transform: translateY(-2px);
  }

  /* Today cell highlight */
  .weekly-col.today {
    background: rgba(56, 189, 248, 0.06);
    border-color: rgba(56, 189, 248, 0.2);
    box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.15), 0 4px 16px rgba(56, 189, 248, 0.06);
  }

  /* Closed day cell */
  .weekly-col.closed-day {
    border-color: rgba(239, 68, 68, 0.1);
    background: rgba(239, 68, 68, 0.03);
  }
  .weekly-col.closed-day:hover {
    border-color: rgba(239, 68, 68, 0.2);
    background: rgba(239, 68, 68, 0.06);
  }

  /* TODAY pill */
  .weekly-col .w-today-tag {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 8px;
    font-size: 0.48rem;
    font-weight: 800;
    background: linear-gradient(135deg, var(--accent), #22d3ee);
    color: var(--bg-deep);
    letter-spacing: 0.3px;
    margin-bottom: 6px;
    text-transform: uppercase;
  }

  /* Day name (월, 화, 수...) — calendar header */
  .weekly-col .w-day {
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--text-dim);
    margin-bottom: 1px;
    letter-spacing: 0.3px;
  }
  .weekly-col.today .w-day { color: var(--accent); }
  .weekly-col.closed-day .w-day { color: #f87171; }

  /* Date (5.20) */
  .weekly-col .w-date {
    font-size: 0.85rem;
    color: rgba(126, 147, 176, 0.6);
    font-weight: 600;
    margin-bottom: 10px;
  }
  .weekly-col.today .w-date { color: rgba(56, 189, 248, 0.4); }
  .weekly-col.closed-day .w-date { color: rgba(248, 113, 113, 0.4); }

  /* Operating hours */
  .weekly-col .w-hours {
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--text);
    letter-spacing: -0.2px;
    margin-bottom: 8px;
    font-variant-numeric: tabular-nums;
  }
  .weekly-col.today .w-hours { color: var(--text-bright); }
  .weekly-col.closed-day .w-hours { color: rgba(248, 113, 113, 0.5); }

  /* Status badge (운영 / 휴장) */
  .weekly-col .w-status {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 12px;
    border-radius: 10px;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.3px;
  }
  .weekly-col .w-status.open {
    background: rgba(34, 197, 94, 0.08);
    color: #4ade80;
    border: 1px solid rgba(34, 197, 94, 0.12);
  }
  .weekly-col .w-status.open::before {
    content: '';
    display: inline-block;
    width: 5px; height: 5px;
    border-radius: 50%;
    background: #4ade80;
  }
  .weekly-col .w-status.closed {
    background: rgba(239, 68, 68, 0.08);
    color: #f87171;
    border: 1px solid rgba(239, 68, 68, 0.12);
  }
  .weekly-col .w-status.closed::before {
    content: '✕';
    font-size: 0.5rem;
    font-weight: 800;
  }

  /* ── Scroll hint ───────────────────────────────────── */
  .scroll-hint {
    display: none;
    text-align: center;
    font-size: 0.68rem;
    color: rgba(126, 147, 176, 0.3);
    margin-top: 6px;
    letter-spacing: 1.5px;
    animation: fade-in 0.6s ease 1.2s both;
  }

  /* ── Footer ───────────────────────────────────────────── */
  footer {
    text-align: center;
    margin-top: 56px;
    padding: 28px 0 14px;
    border-top: 1px solid rgba(56, 189, 248, 0.04);
    font-size: 0.78rem;
    color: var(--text-dim);
    line-height: 1.8;
  }
  footer a {
    color: var(--accent);
    text-decoration: none;
    transition: color 0.2s;
  }
  footer a:hover {
    color: var(--text-bright);
    text-decoration: underline;
  }

  /* ── Loading / Error states ───────────────────────────── */
  .loading {
    text-align: center;
    padding: 100px 20px;
    color: var(--text-dim);
    animation: fade-in 0.5s ease;
  }
  .loading .spinner {
    width: 44px; height: 44px;
    border: 3px solid rgba(30, 50, 80, 0.4);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 24px;
  }
  .loading .spinner-text {
    font-size: 0.88rem;
    color: rgba(126, 147, 176, 0.6);
  }

  .error-state {
    text-align: center;
    padding: 100px 20px;
    animation: fade-in 0.4s ease;
  }
  .error-state .icon { font-size: 3rem; margin-bottom: 16px; display: block; }
  .error-state .msg {
    color: #f87171;
    font-size: 0.95rem;
  }
  .error-state .msg small {
    opacity: 0.6;
    font-size: 0.8rem;
  }
  .error-state .retry-btn {
    margin-top: 24px;
    padding: 12px 32px;
    border: 1px solid rgba(248, 113, 113, 0.2);
    background: rgba(248, 113, 113, 0.06);
    color: #f87171;
    border-radius: 12px;
    cursor: pointer;
    font-size: 0.88rem;
    font-weight: 600;
    transition: all 0.25s;
    font-family: inherit;
    letter-spacing: 0.3px;
  }
  .error-state .retry-btn:hover {
    background: rgba(248, 113, 113, 0.12);
    border-color: rgba(248, 113, 113, 0.4);
    transform: translateY(-1px);
  }

  /* ── Responsive ───────────────────────────────────────── */
  @media (hover: none) and (pointer: coarse) {
    .forecast-item, .info-item, .retry-btn { cursor: default; -webkit-tap-highlight-color: transparent; touch-action: manipulation; }
    .forecast-item:active { transform: scale(0.96) !important; transition: transform 0.15s !important; }
    .info-item:active { transform: scale(0.98) !important; }
    .retry-btn:active { transform: scale(0.97) !important; }
  }

  @media (max-width: 640px) {
    .last-updated {
      bottom: 14px;
      right: 18px;
      font-size: 0.55rem;
    }
    @supports(padding:max(0px)) {
      .container { padding: 16px max(16px, env(safe-area-inset-left)) 40px max(16px, env(safe-area-inset-right)); }
    }
    .container { padding: 16px 16px 40px; }
    header { padding: 24px 0 8px; }
    header::after { width: 50px; margin: 14px auto 0; }
    header h1 { font-size: 1.4rem; }
    header p { font-size: 0.8rem; }
    .weekly-card { padding: 18px 14px 16px; }
    .weekly-strip { gap: 6px; }
    .weekly-col { padding: 10px 4px 10px; max-width: none; }
    .weekly-col .w-today-tag { font-size: 0.42rem; padding: 1px 8px; margin-bottom: 4px; }
    .weekly-col .w-day { font-size: 0.72rem; }
    .weekly-col .w-date { font-size: 0.75rem; margin-bottom: 6px; }
    .weekly-col .w-hours { font-size: 0.65rem; margin-bottom: 6px; }
    .weekly-col .w-status { font-size: 0.55rem; padding: 2px 8px; }
    .weekly-col .w-status.open::before,
    .weekly-col .w-status.closed::before { width: 4px; height: 4px; font-size: 0.42rem; }
    .time-bar {
      padding: 12px 18px;
      gap: 10px;
      font-size: 0.95rem;
      width: 100%;
      border-radius: 30px;
      margin: 18px auto 28px;
    }
    #clock-display { font-size: 1rem; }
    #date-display { font-size: 0.75rem; }
    .gauge-card { padding: 30px 20px 26px; }
    .gauge-ring { width: 170px; height: 170px; }
    .gauge-ring svg { width: 170px; height: 170px; }
    .gauge-label .pct { font-size: 2.5rem; }
    .gauge-label .pct-label { font-size: 0.9rem; }
    .gauge-status-badge {
      top: 12px;
      right: 14px;
      padding: 6px 14px;
      font-size: 0.7rem;
      min-height: 36px;
    }
    .source-badge {
      bottom: 14px;
      left: 18px;
      font-size: 0.58rem;
    }
    .info-grid { grid-template-columns: 1fr 1fr; gap: 12px; margin: 28px 0; }
    .info-item { padding: 16px 18px; min-height: 52px; }
    .info-item .value { font-size: 0.85rem; }
    .hours-combined { padding: 10px 16px; gap: 10px; }
    .hours-label { font-size: 0.58rem; }
    .hours-value { font-size: 0.68rem; }
    .hours-divider { height: 16px; }
    .gender-rates { padding: 16px 18px; gap: 14px; margin: 20px auto 0; }
    .gender-bar-track { height: 14px; }
    .gender-row { gap: 10px; min-height: 40px; }
    .section-title { font-size: 0.95rem; margin-bottom: 14px; }
    .weekly-section { margin: 24px 0; }
    .forecast-section { margin-top: 28px; }
    .gauge-tip { margin-top: 18px; padding: 14px 18px; }
    footer { margin-top: 44px; padding: 22px 0 12px; }

    /* Forecast: horizontal scroll on mobile */
    .forecast-scroll-wrap { margin: 0 -16px; }
    .forecast-scroll-wrap .forecast-grid {
      display: flex;
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
      gap: 10px;
      padding: 4px 16px 12px;
      scrollbar-width: thin;
    }
    .forecast-scroll-wrap .forecast-grid::-webkit-scrollbar { height: 4px; }
    .forecast-scroll-wrap .forecast-grid::-webkit-scrollbar-track { background: transparent; }
    .forecast-scroll-wrap .forecast-grid::-webkit-scrollbar-thumb {
      background: rgba(56, 189, 248, 0.15);
      border-radius: 4px;
    }
    .forecast-scroll-wrap .forecast-item {
      scroll-snap-align: start;
      min-width: 72px;
      padding: 10px 4px 10px;
      min-height: 86px;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    .forecast-scroll-wrap .forecast-item:last-child { scroll-snap-align: end; }
    @media (hover: hover) {
      .forecast-scroll-wrap .forecast-item:hover {
        transform: translateY(-3px) scale(1.04);
      }
    }
    .scroll-hint { display: block; }

    .forecast-item .bar { width: 16px; }
    .forecast-item .bar-wrap { height: 40px; }
    .forecast-item .hour { font-size: 0.68rem; }
    .forecast-item .f-label { font-size: 0.66rem; }
    .forecast-item .bar::before {
      top: -2px; left: -3px;
      right: -3px; height: 5px;
    }
  }

  @media (max-width: 420px) {
    .info-grid { grid-template-columns: 1fr; gap: 10px; }
    .weekly-card { padding: 14px 10px 12px; }
    .weekly-strip { gap: 4px; }
    .weekly-col { padding: 8px 3px 8px; max-width: none; }
    .weekly-col .w-today-tag { font-size: 0.38rem; padding: 1px 6px; margin-bottom: 3px; }
    .weekly-col .w-day { font-size: 0.65rem; }
    .weekly-col .w-date { font-size: 0.65rem; margin-bottom: 4px; }
    .weekly-col .w-hours { font-size: 0.58rem; margin-bottom: 4px; }
    .weekly-col .w-status { font-size: 0.5rem; padding: 1px 6px; }
    .weekly-col .w-status.open::before,
    .weekly-col .w-status.closed::before { width: 3px; height: 3px; font-size: 0.38rem; }
    .gender-rates { max-width: 100%; padding: 14px 16px; }
    .time-bar { flex-wrap: wrap; justify-content: center; gap: 8px; padding: 10px 14px; margin: 16px auto 24px; }
    .gauge-card { padding: 26px 16px 22px; }
    .gauge-ring { width: 150px; height: 150px; }
    .gauge-ring svg { width: 150px; height: 150px; }
    .gauge-label .pct { font-size: 2.2rem; }
    .gauge-label .pct-label { font-size: 0.85rem; }
    .gauge-tip { font-size: 0.82rem; padding: 14px 16px; margin-top: 16px; }
    .gauge-status-badge { top: 10px; right: 12px; padding: 5px 12px; font-size: 0.68rem; min-height: 32px; }
    .source-badge { bottom: 10px; left: 14px; font-size: 0.55rem; padding: 3px 10px; }
    footer { font-size: 0.72rem; margin-top: 38px; padding: 20px 0 10px; }
    .section-title { font-size: 0.9rem; margin-bottom: 12px; }
    .forecast-section { margin-top: 24px; }
    .weekly-section { margin: 20px 0; }
  }

  @media (max-width: 360px) {
    @supports(padding:max(0px)) {
      .container { padding: 12px max(12px, env(safe-area-inset-left)) 36px max(12px, env(safe-area-inset-right)); }
    }
    .container { padding: 12px 12px 36px; }
    header { padding: 20px 0 6px; }
    header h1 { font-size: 1.2rem; }
    header p { font-size: 0.75rem; }
    .weekly-card { padding: 12px 8px 10px; }
    .weekly-strip { gap: 3px; }
    .weekly-col { padding: 6px 2px 6px; max-width: none; }
    .weekly-col .w-today-tag { font-size: 0.34rem; padding: 0 5px; margin-bottom: 2px; }
    .weekly-col .w-day { font-size: 0.58rem; }
    .weekly-col .w-date { font-size: 0.6rem; margin-bottom: 3px; }
    .weekly-col .w-hours { font-size: 0.5rem; margin-bottom: 3px; }
    .weekly-col .w-status { font-size: 0.45rem; padding: 1px 5px; }
    .weekly-col .w-status.open::before,
    .weekly-col .w-status.closed::before { width: 3px; height: 3px; font-size: 0.34rem; }
    .time-bar { font-size: 0.85rem; gap: 6px; padding: 8px 12px; margin: 14px auto 20px; }
    #clock-display { font-size: 0.85rem; }
    #date-display { font-size: 0.68rem; }
    .gauge-card { padding: 22px 14px 20px; }
    .gauge-ring { width: 130px; height: 130px; }
    .gauge-ring svg { width: 130px; height: 130px; }
    .gauge-label .pct { font-size: 2rem; }
    .gauge-label .pct-label { font-size: 0.8rem; }
    .gauge-status-badge { top: 8px; right: 10px; padding: 4px 10px; font-size: 0.62rem; min-height: 28px; }
    .source-badge { bottom: 8px; left: 12px; font-size: 0.52rem; padding: 3px 8px; }
    .info-item { padding: 12px 14px; min-height: 44px; }
    .info-item .value { font-size: 0.8rem; }
    .hours-combined { padding: 8px 12px; gap: 8px; }
    .hours-label { font-size: 0.55rem; }
    .hours-value { font-size: 0.65rem; }
    .hours-divider { height: 14px; }
    .info-item .label { font-size: 0.63rem; }
    .gender-rates { padding: 12px 14px; gap: 10px; margin: 16px auto 0; }
    .gender-bar-track { height: 12px; }
    .gender-row { gap: 8px; min-height: 34px; }
    .gender-icon { font-size: 0.9rem; }
    .gender-label { font-size: 0.7rem; width: 34px; }
    .gender-value { font-size: 0.75rem; width: 36px; }
    .section-title { font-size: 0.85rem; margin-bottom: 10px; }
    .weekly-section { margin: 16px 0; }
    .forecast-section { margin-top: 20px; }
    .gauge-tip { font-size: 0.8rem; padding: 12px 14px; margin-top: 14px; }
    footer { font-size: 0.7rem; margin-top: 32px; padding: 16px 0 8px; }
    .forecast-item { min-width: 62px; padding: 8px 3px 8px; min-height: 76px; }
    .forecast-item .hour { font-size: 0.6rem; margin-bottom: 4px; }
    .forecast-item .bar { width: 14px; }
    .forecast-item .bar-wrap { height: 34px; margin-bottom: 3px; }
    .forecast-item .f-label { font-size: 0.58rem; }
    .error-state .retry-btn { padding: 10px 24px; font-size: 0.82rem; }
    .loading { padding: 60px 14px; }
    .loading .spinner { width: 36px; height: 36px; }
  }
</style>
</head>
<body>

<!-- Floating orbs -->
<div class="orb"></div>
<div class="orb"></div>
<div class="orb"></div>

<!-- Wave animation SVG -->
<div class="wave-overlay">
  <svg viewBox="0 0 1440 120" preserveAspectRatio="none">
    <path d="M0,60 C360,120 720,0 1080,60 C1260,90 1350,60 1440,60 L1440,120 L0,120 Z" fill="var(--accent)"/>
  </svg>
  <svg viewBox="0 0 1440 120" preserveAspectRatio="none">
    <path d="M0,60 C360,0 720,90 1080,30 C1260,10 1350,40 1440,50 L1440,120 L0,120 Z" fill="var(--accent)"/>
  </svg>
</div>

<div class="container" id="app">
  <!-- Loading state -->
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div class="spinner-text">
      혼잡도 정보를 불러오는 중...
    </div>
  </div>

  <!-- Error state -->
  <div class="error-state" id="error" style="display:none">
    <span class="icon">⚠️</span>
    <div class="msg">
      데이터를 불러오지 못했습니다.<br><small id="error-msg"></small>
    </div>
    <button class="retry-btn" onclick="fetchData()">다시 시도</button>
  </div>

  <!-- Main content (hidden initially) -->
  <div id="content" style="display:none">

    <header class="animate-in">
      <h1><span class="pool-emoji">🏊</span> <span class="accent-text">라온</span> 수영장 혼잡도</h1>
      <p>전주 라온체육센터 · 25m 6레인</p>
    </header>

    <div class="time-bar animate-in animate-in-delay-1">
      <span class="live-dot"></span>
      <span id="date-display"></span>
      <span id="clock-display">--:--:--</span>
      <span class="day-badge" id="day-badge">--</span>
    </div>

    <!-- Closure banner -->
    <div class="closed-banner" id="closed-banner">
      <span class="cb-icon">🔒</span>
      <div class="cb-title">오늘은 휴장일입니다</div>
      <div class="cb-desc" id="closed-desc">수영장을 이용할 수 없습니다.</div>
      <div class="cb-closure" id="closed-reason">--</div>
    </div>

    <!-- Main gauge -->
    <div class="gauge-card glass-card animate-in animate-in-delay-2" id="gauge-card">
      <div class="gauge-status-badge" id="status-badge">
        <span class="status-dot"></span>
        <span id="status-text">--</span>
      </div>
      <div class="gauge-ring">
        <svg viewBox="0 0 120 120" width="200" height="200">
          <circle class="bg-circle" cx="60" cy="60" r="50"/>
          <circle class="fg-circle" id="gauge-circle" cx="60" cy="60" r="50"
                  stroke="#22c55e"
                  stroke-dasharray="314.16"
                  stroke-dashoffset="0"/>
        </svg>
        <div class="gauge-label">
          <div class="pct" id="level-pct">--%</div>
          <div class="pct-label" id="level-label">--</div>
        </div>
      </div>

      <!-- Data source -->
      <div class="source-badge heuristic" id="source-badge">예측</div>

      <!-- Last updated timestamp -->
      <div class="last-updated" id="last-updated" style="display:none"></div>

      <!-- Gender usage rates -->
      <div class="gender-rates">
        <div class="gender-row">
          <span class="gender-icon">♂</span>
          <span class="gender-label">남성</span>
          <div class="gender-bar-track">
            <div class="gender-bar-fill male" id="male-bar" style="width:0%"></div>
          </div>
          <span class="gender-value" id="male-rate">0%</span>
        </div>
        <div class="gender-row">
          <span class="gender-icon">♀</span>
          <span class="gender-label">여성</span>
          <div class="gender-bar-track">
            <div class="gender-bar-fill female" id="female-bar" style="width:0%"></div>
          </div>
          <span class="gender-value" id="female-rate">0%</span>
        </div>
      </div>

      <div class="gauge-tip" id="tip">
        팁이 로딩 중입니다...
      </div>
    </div>

    <!-- Pool info -->
    <div class="info-grid animate-in animate-in-delay-3">
      <div class="info-item hours-combined" id="hours-card">
        <div class="hours-row">
          <span class="hours-label">🕐</span>
          <span class="hours-value" id="weekday-hours">--</span>
        </div>
        <div class="hours-divider"></div>
        <div class="hours-row">
          <span class="hours-label">🕐</span>
          <span class="hours-value" id="weekend-hours">--</span>
        </div>
      </div>
    </div>

    <!-- Weekly schedule (single strip) -->
    <div class="weekly-section animate-in animate-in-delay-4">
      <div class="section-title">
        <span>📅</span> 주간 운영 현황
      </div>
      <div class="weekly-card glass-card" id="weekly-card">
        <!-- Filled by JS -->
      </div>
    </div>

    <!-- Hourly forecast + trend (integrated) -->
    <div class="forecast-section animate-in animate-in-delay-4">
      <div class="section-title">
        <span>📊</span> 시간대별 예상 혼잡도
        <span class="badge-count" id="forecast-count">13시간</span>
      </div>
      <div class="forecast-scroll-wrap" id="forecast-scroll-wrap">
        <div class="forecast-grid" id="forecast-grid">
          <!-- Filled by JS -->
        </div>
        <div class="scroll-hint">← 좌우로 스크롤 →</div>
      </div>
    </div>

    <footer class="animate-in animate-in-delay-5" style="animation-delay:0.5s">
      <p>⏱ <strong id="footer-source">실시간</strong> · 전주시설관리공단 <a href="https://www.jjss.or.kr/reserv/index.9is?contentUid=5232d76d8f95414801904883b431549b&amp;searchType=PL004&amp;subPath=" target="_blank">실시간 현황</a>을 반영합니다.</p>
      <p style="margin-top:4px">
        전주시시설관리공단 ·
        <a href="https://map.naver.com/p/search/%EC%A0%84%EC%A3%BC%20%EB%9D%BC%EC%98%A8%EC%B2%B4%EC%9C%A1%EC%84%BC%ED%84%B0/place/1181720929" target="_blank">네이버 지도</a> ·
        <a href="https://www.jjss.or.kr/reserv/index.9is?contentUid=5232d76d8f95414801904883b431549b&searchType=PL004&subPath=" target="_blank" style="font-weight:600">실시간 예약 페이지</a>
      </p>
    </footer>
  </div>
</div>

<script>
// ── Gauge helpers ───────────────────────────────────────────────────────────
const CIRCUMFERENCE = 2 * Math.PI * 50;  // 314.159…

function setGauge(pct, color) {
  const circle = document.getElementById('gauge-circle');
  const offset = CIRCUMFERENCE - (pct / 100) * CIRCUMFERENCE;
  circle.style.strokeDashoffset = offset;
  circle.style.stroke = color;

  // Glow effect based on level
  const glow = document.querySelector('.gauge-ring svg');
  if (pct >= 70) {
    glow.style.setProperty('--gauge-glow', color + '77');
  } else if (pct >= 50) {
    glow.style.setProperty('--gauge-glow', color + '55');
  } else {
    glow.style.setProperty('--gauge-glow', 'transparent');
  }

  // Pulse animation for high congestion (only when open)
  const statusText = document.getElementById('status-text').textContent;
  const gaugeCard = document.querySelector('.gauge-card');
  if (pct >= 70 && statusText === '운영중') {
    gaugeCard.style.setProperty('--gauge-glow', color + '33');
    gaugeCard.style.animation = 'pulse-glow 2s ease-in-out infinite';
  } else {
    gaugeCard.style.animation = 'none';
  }

  // Tip border color
  const tip = document.getElementById('tip');
  tip.style.borderColor = color;
  tip.style.background = color + '0a';
}

// ── Number counting animation ────────────────────────────────────────────────
function animateNumber(el, target, suffix, color) {
  const duration = 900;
  const start = performance.now();
  const startVal = parseInt(el.textContent) || 0;

  function tick(now) {
    const elapsed = now - start;
    const progress = Math.min(elapsed / duration, 1);
    // Elastic ease-out for more dramatic effect
    const eased = progress < 0.5
      ? 4 * progress * progress * progress
      : 1 - Math.pow(-2 * progress + 2, 3) / 2;
    const current = Math.round(startVal + (target - startVal) * eased);
    el.textContent = current + suffix;
    el.style.color = color;
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Animate gender bar width ────────────────────────────────────────────────
function animateBar(el, target) {
  const duration = 1000;
  const start = performance.now();
  const startVal = parseFloat(el.style.width) || 0;
  function tick(now) {
    const elapsed = now - start;
    const progress = Math.min(elapsed / duration, 1);
    // Elastic ease-out
    const eased = progress < 0.5
      ? 4 * progress * progress * progress
      : 1 - Math.pow(-2 * progress + 2, 3) / 2;
    el.style.width = (startVal + (target - startVal) * eased) + '%';
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Korean Time (KST, UTC+9) helpers ────────────────────────────────────────
function getKST() {
  const now = new Date();
  const utc = now.getTime() + now.getTimezoneOffset() * 60000;
  return new Date(utc + 9 * 3600000);
}

// ── Live clock & date ────────────────────────────────────────────────────────
function updateClock() {
  const now = getKST();
  const h = String(now.getHours()).padStart(2, '0');
  const m = String(now.getMinutes()).padStart(2, '0');
  const s = String(now.getSeconds()).padStart(2, '0');
  document.getElementById('clock-display').textContent = `${h}:${m}:${s}`;
}

const DAY_NAMES = ['일', '월', '화', '수', '목', '금', '토'];
function updateDateDisplay(dateStr) {
  const el = document.getElementById('date-display');
  if (dateStr) {
    const d = new Date(dateStr.replace(' ', 'T') + '+09:00');
    if (!isNaN(d)) {
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      el.textContent = `${y}.${m}.${day} (${DAY_NAMES[d.getDay()]})`;
      return;
    }
  }
  // Fallback to KST
  const d = getKST();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  el.textContent = `${y}.${m}.${day} (${DAY_NAMES[d.getDay()]})`;
}

// ── Status badge ─────────────────────────────────────────────────────────────
function setStatusBadge(status) {
  const badge = document.getElementById('status-badge');
  const text = document.getElementById('status-text');
  text.textContent = status;
  badge.className = 'gauge-status-badge';
  if (status === '운영중') {
    badge.classList.add('open');
  } else if (status === '수질정화시간') {
    badge.classList.add('break');
  } else if (status === '휴장') {
    badge.classList.add('closed-day');
  } else {
    badge.classList.add('closed');
  }
}

// ── Gender rates ────────────────────────────────────────────────────────────
function setGenderRates(male, female) {
  const maleBar = document.getElementById('male-bar');
  const femaleBar = document.getElementById('female-bar');
  document.getElementById('male-rate').textContent = male + '%';
  document.getElementById('female-rate').textContent = female + '%';
  animateBar(maleBar, Math.min(male, 100));
  animateBar(femaleBar, Math.min(female, 100));
}

// ── Forecast renderer ─────────────────────────────────────────────────────────
function renderForecast(forecast) {
  const grid = document.getElementById('forecast-grid');
  grid.innerHTML = '';

  forecast.forEach((f, i) => {
    const div = document.createElement('div');
    div.className = 'forecast-item';
    div.style.animationDelay = `${i * 0.05}s`;

    // Color with alpha for the bar
    const barBg = f.color + 'cc';

    div.innerHTML = `
      <div class="hour">${f.hour}</div>
      <div class="bar-wrap">
        <div class="bar" style="height:3px;background:${barBg}"></div>
      </div>
      <div class="f-label" style="color:${f.color}">${f.level}%</div>
    `;
    grid.appendChild(div);

    requestAnimationFrame(() => {
      const bar = div.querySelector('.bar');
      if (bar) {
        bar.style.height = `${f.level * 0.36 + 4}px`;
      }
    });
  });

  document.getElementById('forecast-count').textContent = `${forecast.length}시간`;
}  // ── Weekly schedule renderer (calendar) ────────────────────────────────────
function renderWeeklySchedule(schedule) {
  const card = document.getElementById('weekly-card');
  const strip = document.createElement('div');
  strip.className = 'weekly-strip';

  schedule.forEach((d, i) => {
    const col = document.createElement('div');
    col.className = 'weekly-col';
    if (d.is_today) col.classList.add('today');
    if (d.is_closed) col.classList.add('closed-day');
    col.style.animationDelay = `${i * 0.05}s`;

    const compactHours = d.is_closed ? '--:--' : d.hours.replace(/:00/g, '');

    col.innerHTML = `
      ${d.is_today ? '<div class="w-today-tag">TODAY</div>' : ''}
      <div class="w-day">${d.day_name}</div>
      <div class="w-date">${d.date}</div>
      <div class="w-hours">${d.is_closed ? '--:--' : compactHours}</div>
      <div class="w-status ${d.is_closed ? 'closed' : 'open'}">${d.is_closed ? '휴장' : '운영'}</div>
    `;

    strip.appendChild(col);
  });

  card.innerHTML = '';
  card.appendChild(strip);
}

// ── Fetch weekly schedule ───────────────────────────────────────────────────
async function fetchWeeklySchedule() {
  try {
    const res = await fetch('/api/weekly-schedule');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderWeeklySchedule(data.schedule);
  } catch (err) {
    console.warn('Weekly schedule fetch failed:', err.message);
  }
}

// ── Mobile scroll hint auto-hide ────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const wrap = document.getElementById('forecast-scroll-wrap');
  if (!wrap) return;
  const grid = wrap.querySelector('.forecast-grid');
  const hint = wrap.querySelector('.scroll-hint');
  if (!grid || !hint) return;
  grid.addEventListener('scroll', () => {
    if (hint.style.opacity !== '0') {
      hint.style.transition = 'opacity 0.4s ease';
      hint.style.opacity = '0';
      hint.style.pointerEvents = 'none';
    }
  }, { once: true });
});

// ── Fetch & render all data ──────────────────────────────────────────────────
async function fetchData() {
  try {
    const res = await fetch('/api/congestion');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    document.getElementById('loading').style.display = 'none';
    document.getElementById('error').style.display = 'none';
    const content = document.getElementById('content');

    const isFirstLoad = content.style.display === 'none';
    content.style.display = 'block';

    // Date & clock
    updateDateDisplay(data.time);
    document.getElementById('clock-display').textContent = data.time.split(' ')[1] + ':00';

    // Day badge
    const badge = document.getElementById('day-badge');
    if (data.current.is_weekend) {
      badge.textContent = '주말';
      badge.className = 'day-badge weekend';
    } else {
      badge.textContent = '평일';
      badge.className = 'day-badge weekday';
    }

    // Closure banner
    const closedBanner = document.getElementById('closed-banner');
    const closedReason = document.getElementById('closed-reason');
    const closedDesc = document.getElementById('closed-desc');
    if (data.current.is_closed) {
      closedBanner.style.display = 'block';
      closedReason.textContent = data.current.closed_reason || '정기휴장';
      closedDesc.textContent = '오늘은 수영장 휴장일입니다. 운영 시간에 방문해주세요.';
      // Show status as closed in the gauge
      document.getElementById('level-pct').textContent = '휴장';
      document.getElementById('level-pct').style.color = '#ef4444';
      document.getElementById('level-label').textContent = '오늘은 쉬는 날';
      document.getElementById('level-label').style.color = '#f87171';
      // Hide gender rates on closed days
      document.querySelector('.gender-rates').style.display = 'none';
      // Hide tip when closed
      document.getElementById('tip').style.display = 'none';
      // Hide source badge
      document.getElementById('source-badge').style.display = 'none';
    } else {
      closedBanner.style.display = 'none';
      document.querySelector('.gender-rates').style.display = '';
      document.getElementById('tip').style.display = '';
      document.getElementById('source-badge').style.display = '';
    }

    // Status badge
    setStatusBadge(data.current.status);

    // Data source badge
    const sourceBadge = document.getElementById('source-badge');
    const footerSource = document.getElementById('footer-source');
    const lastUpdated = document.getElementById('last-updated');
    if (data.current.data_source === 'live') {
      sourceBadge.className = 'source-badge live';
      sourceBadge.textContent = '실시간';
      footerSource.textContent = '실시간';
      // Show last updated time
      if (data.current.last_updated) {
        const now = getKST();
        const updated = new Date(data.current.last_updated.replace(' ', 'T') + '+09:00');
        const diffSec = Math.floor((now - updated) / 1000);
        let timeAgo;
        if (diffSec < 60) {
          timeAgo = '방금 전';
        } else if (diffSec < 3600) {
          timeAgo = Math.floor(diffSec / 60) + '분 전';
        } else {
          timeAgo = Math.floor(diffSec / 3600) + '시간 전';
        }
        lastUpdated.textContent = '⏱ ' + timeAgo;
        lastUpdated.className = 'last-updated live';
        lastUpdated.style.display = '';
      }
    } else {
      sourceBadge.className = 'source-badge heuristic';
      sourceBadge.textContent = '예측';
      footerSource.textContent = '예측';
      lastUpdated.style.display = 'none';
    }

    // Gender rates
    setGenderRates(data.current.male_rate, data.current.female_rate);

    // Gauge (skip on closed days — closure banner handles display)
    if (!data.current.is_closed) {
      const c = data.current;
      const pctEl = document.getElementById('level-pct');
      const labelEl = document.getElementById('level-label');

      if (isFirstLoad) {
        pctEl.textContent = '0%';
        labelEl.textContent = c.label;
        labelEl.style.color = c.color;
        animateNumber(pctEl, c.level, '%', c.color);
      } else {
        animateNumber(pctEl, c.level, '%', c.color);
        labelEl.textContent = c.label;
        labelEl.style.color = c.color;
      }

      setGauge(c.level, c.color);
      document.getElementById('tip').innerHTML = c.tip;
    }

    // Pool info
    const p = data.pool;
    document.getElementById('weekday-hours').textContent = p.weekday_hours;
    document.getElementById('weekend-hours').textContent = p.weekend_hours;

    // Forecast
    renderForecast(data.forecast);

  } catch (err) {
    document.getElementById('loading').style.display = 'none';
    const el = document.getElementById('error');
    el.style.display = 'block';
    document.getElementById('error-msg').textContent = err.message;
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────

updateClock();
setInterval(updateClock, 1000);

fetchData();
fetchWeeklySchedule();

setInterval(fetchData, 60000);
setInterval(fetchWeeklySchedule, 300000); // Refresh weekly schedule every 5 min
</script>
</body>
</html>
"""


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
