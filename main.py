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


# ── Known historical patterns (from real jjss.or.kr data analysis) ──────────
# Raw hourly user counts observed from the actual website.
# Used as default predictions when live scraping is unavailable.
_KNOWN_HISTORICAL_PATTERNS: dict = {
    "weekday": {
        # Updated from latest scrape data (June 2, 2026 09:32 KST)
        # Pattern: morning PEAK at 06시 (93 users) → gradual decline →
        #          lunch break → afternoon moderate → evening very quiet
        # Note: Data varies daily. The RELATIVE pattern is consistent.
        6: 93, 7: 29, 8: 42, 9: 22, 10: 40,
        11: 5, 12: 4, 13: 39, 14: 16, 15: 22,
        16: 20, 17: 17, 18: 2, 19: 1, 20: 1,
    },
    "saturday": {
        # Estimated based on typical Saturday pool usage patterns
        # (no direct observation data yet)
        6: 30, 7: 35, 8: 50, 9: 60, 10: 70,
        11: 65, 12: 60, 13: 55, 14: 45, 15: 35,
        16: 25, 17: 15,
    },
    "sunday": {
        # Based on Sunday May 31, 2026 — actual observed user counts from history page
        # Pattern: late start (10시) → afternoon PEAK at 15시 → steep drop
        10: 56, 11: 41, 12: 17, 13: 40, 14: 41,
        15: 60, 16: 7, 17: 2,
    },
}

# Default calibration levels by day type (typical congestion % at current hour)
_DEFAULT_DAY_LEVELS: dict = {
    "weekday": 38,    # 38% actual current utilization (updated from latest scrape)
    "saturday": 55,   # 55% typical for Saturday 14시
    "sunday": 50,     # 50% typical for Sunday 15시
}


def _get_default_forecast(now: datetime) -> dict | None:
    """Generate default congestion forecast using known historical patterns.

    Uses the day-type-matched pattern and calibrates to a default level.
    Returns {hour: level} or None if no pattern matches.
    """
    day_type = _get_day_type(now)
    pattern = _KNOWN_HISTORICAL_PATTERNS.get(day_type)
    if not pattern:
        return None

    current_hour = now.hour

    # Find calibration source: use a reliable mid-range value
    # Avoid tiny estimated values for late hours (which cause extreme factors)
    # Prefer hours with val >= 10 (significant observed data) for stable calibration
    current_val = pattern.get(current_hour, 0)
    if current_val < 10:
        for h in range(current_hour - 1, 5, -1):
            if pattern.get(h, 0) >= 10:
                current_val = pattern[h]
                break
    if current_val < 10:
        # Fallback: find the highest-value hour as a stable reference
        max_hour = max(pattern, key=pattern.get)
        max_val = pattern[max_hour]
        if max_val >= 10:
            current_val = max_val
    if current_val < 3:
        return None  # Can't calibrate with such small values

    default_level = _DEFAULT_DAY_LEVELS.get(day_type, 30)
    factor = default_level / current_val if current_val > 0 else 1.0

    # Generate levels for all known pattern hours
    # The pattern already contains all operating hour keys
    levels = {}
    for hour, val in pattern.items():
        est = round(val * factor)
        est = max(0, min(est, 95))
        levels[hour] = est
    return levels if levels else None


def _init_default_predictions() -> None:
    """Pre-populate historical predictions with known patterns at startup.

    This ensures the forecast works even when live scraping is unavailable
    (e.g., on Vercel where jjss.or.kr blocks requests from overseas IPs).
    """
    global _HISTORICAL_PREDICTIONS, _HISTORICAL_DATE
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    predictions = _get_default_forecast(now)
    if predictions:
        _HISTORICAL_PREDICTIONS.clear()
        _HISTORICAL_PREDICTIONS.update(predictions)
        _HISTORICAL_DATE = now.strftime("%Y-%m-%d")


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

    # Determine label from the "whole bg_xxx" class (total utilization section)
    # whole bg_spare = 여유, whole bg_general = 보통, whole bg_congestion = 혼잡
    status_label = "여유"
    whole_match = _re.search(rb'class="whole bg_([^"]+)"', raw)
    if whole_match:
        cls = whole_match.group(1)
        if cls == b"congestion":
            status_label = "혼잡"
        elif cls == b"general":
            status_label = "보통"
        # else bg_spare → stays "여유"
    else:
        # Fallback: search anywhere on the page
        if _re.search(rb"bg_congestion", raw):
            status_label = "혼잡"
        elif _re.search(rb"bg_general", raw):
            status_label = "보통"

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
        if users == 0:  # Skip hours with no data (future hours)
            continue
        est = round(users * factor)
        est = max(0, min(est, 95))  # Clamp 0-95
        levels[hour] = est
    return levels


def _build_historical_predictions(hist_data: dict, now: datetime) -> dict | None:
    """Build congestion predictions using historical data for the SAME day type.

    Day type matching logic:
    - Weekday (Mon-Fri): uses 'last_week' (same weekday) → "same weekday"
    - Saturday: uses 'last_week' (last Saturday) → "same weekend day"
    - Sunday: uses 'last_week' (last Sunday) → "same weekend day"
    - Holiday: returns None (no matching historical data available)

    Falls back to 'yesterday', then 'today' data when primary source is empty.
    Calibrates historical user counts against current observed utilization.

    Returns {hour: level} or None.
    """
    day_type = _get_day_type(now)
    current_hour = now.hour

    # For holidays, historical matching doesn't work reliably
    # (last_week would be a regular day, not a holiday)
    if day_type == "holiday":
        return None

    # Define column priority based on day type
    # For all types: last_week (same weekday) > yesterday > today
    last_week_current = hist_data.get(current_hour, {}).get("last_week", 0)
    yesterday_current = hist_data.get(current_hour, {}).get("yesterday", 0)
    today_current = hist_data.get(current_hour, {}).get("today", 0)

    pattern_source = None
    pattern_key = None

    # Try each column in priority order
    for col, val in [("last_week", last_week_current),
                     ("yesterday", yesterday_current),
                     ("today", today_current)]:
        if val > 0:
            pattern_source = val
            pattern_key = col
            break

    # Fallback: scan previous hours for ANY non-zero data
    if not pattern_source:
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

    # Calibration factor: current utilization / historical source value
    # Clamp factor to prevent extreme amplification from tiny historical values
    factor = current_level / pattern_source if pattern_source > 0 else 1.0
    factor = max(0.3, min(factor, 3.0))

    levels = {}
    for hour, data in hist_data.items():
        val = data.get(pattern_key, 0)
        if val == 0:
            continue
        est = round(val * factor)
        est = max(0, min(est, 95))
        levels[hour] = est

    return levels if levels else None


# ── Day type classification ──────────────────────────────────────────────

def _get_day_type(now: datetime) -> str:
    """Classify the day into a type for historical matching.

    Returns one of: 'weekday', 'saturday', 'sunday', 'holiday'
    Used to select appropriate historical data for predictions.
    """
    if _is_closed_day(now):
        return "holiday"
    weekday = now.weekday()
    if weekday == 6:
        return "sunday"
    elif weekday == 5:
        return "saturday"
    else:
        return "weekday"


# Call at module load so defaults are available immediately
_init_default_predictions()


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
            # Based on analysis of actual Sunday May 31, 2026 jjss.or.kr data:
            # Late start (10시) → gradual build-up → afternoon PEAK at 15시 (60 users)
            # → steep drop after 15시
            # See _KNOWN_HISTORICAL_PATTERNS for raw data.
            day_type = "일요일·공휴일"
            if 10 <= hour < 11:
                level, label, tip = 45, "보통", "일요일 개장 직후, 방문객이 빠르게 증가합니다."
                male_rate, female_rate, status = 40, 50, "운영중"
            elif 11 <= hour < 12:
                level, label, tip = 40, "보통", "일요일 오전, 가족 단위 방문객이 꾸준합니다."
                male_rate, female_rate, status = 35, 45, "운영중"
            elif 12 <= hour < 13:
                level, label, tip = 20, "여유", "일요일 점심 시간, 비교적 한산합니다."
                male_rate, female_rate, status = 18, 22, "운영중"
            elif 13 <= hour < 15:
                level, label, tip = 55, "보통", "일요일 오후, 방문객이 다시 증가하는 시간입니다."
                male_rate, female_rate, status = 50, 60, "운영중"
            elif 15 <= hour < 16:
                level, label, tip = 65, "보통", "⚠️ 일요일 오후 피크 시간입니다. 가장 혼잡하니 참고하세요!"
                male_rate, female_rate, status = 60, 70, "운영중"
            elif 16 <= hour <= 17:
                level, label, tip = 25, "여유", "일요일 오후 늦게, 방문객이 급감합니다."
                male_rate, female_rate, status = 20, 30, "운영중"
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
        # Based on analysis of actual Monday June 1, 2026 jjss.or.kr data:
        # Morning PEAK at 06시 (88 users) → gradual decline → lunch break →
        # afternoon moderate → evening very quiet (0 users after 17시)
        # See _KNOWN_HISTORICAL_PATTERNS for raw data.
        day_type = "평일"
        if 6 <= hour < 8:
            level, label, tip = 55, "보통", "🌅 아침 시간대, 평일 중 가장 혼잡합니다. 개장 직후 많은 이용객이 방문해요!"
            male_rate, female_rate, status = 55, 55, "운영중"
        elif 8 <= hour < 10:
            level, label, tip = 45, "보통", "오전 시간, 아침 피크가 지나며 방문객이 점차 감소합니다."
            male_rate, female_rate, status = 45, 45, "운영중"
        elif 10 <= hour < 11:
            level, label, tip = 40, "보통", "오전 늦게, 비교적 여유로운 시간입니다."
            male_rate, female_rate, status = 40, 40, "운영중"
        elif 11 <= hour < 12:
            level, label, tip = 12, "여유", "점심 전 가장 한적한 시간입니다."
            male_rate, female_rate, status = 10, 14, "운영중"
        elif 12 <= hour < 13:
            level, label, tip = 0, "수질정화시간", "⚠️ 수질정화시간(12:00~13:00)입니다. 시설 정비 시간이니 참고하세요."
            male_rate, female_rate, status = 0, 0, "수질정화시간"
        elif 13 <= hour < 14:
            level, label, tip = 45, "보통", "점심 이후, 오후 피크 시간입니다. 방문객이 다시 증가합니다."
            male_rate, female_rate, status = 45, 45, "운영중"
        elif 14 <= hour < 15:
            level, label, tip = 25, "여유", "오후 시간대, 비교적 여유롭습니다."
            male_rate, female_rate, status = 23, 27, "운영중"
        elif 15 <= hour < 16:
            level, label, tip = 22, "여유", "오후 늦게, 한적하게 이용할 수 있습니다."
            male_rate, female_rate, status = 20, 24, "운영중"
        elif 16 <= hour < 18:
            level, label, tip = 28, "여유", "오후 늦은 시간, 평일 중 가장 여유로운 시간대입니다."
            male_rate, female_rate, status = 22, 31, "운영중"
        elif 18 <= hour <= 20:
            level, label, tip = 12, "여유", "🌙 저녁 시간, 가벼운 운동하기 좋습니다. 방문객이 거의 없어요."
            male_rate, female_rate, status = 10, 14, "운영중"
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


def _apply_levels(entries: List[Dict], predictions: Dict, max_delta: int = 20) -> None:
    """Override forecast/trend entries with prediction levels, clamped to max_delta.

    For each entry whose hour matches a key in predictions, the entry's level
    is replaced with the predicted level, clamped so it cannot deviate more
    than max_delta points from the original heuristic baseline.
    Label and color are updated to match the clamped level.
    """
    for entry in entries:
        hour_str = entry.get("hour", "")
        if not hour_str or ":" not in hour_str:
            continue
        try:
            hour = int(hour_str.split(":")[0])
        except (ValueError, IndexError):
            continue
        if hour not in predictions:
            continue
        orig = entry["level"]
        pred = predictions[hour]
        clamped = max(orig - max_delta, min(pred, orig + max_delta))
        clamped = max(0, min(clamped, 100))
        entry["level"] = clamped
        if clamped < 30:
            entry["label"] = "\uc5ec\uc720"
            entry["color"] = "#22c55e"
        elif clamped < 50:
            entry["label"] = "\ubcf4\ud1b5"
            entry["color"] = "#eab308"
        elif clamped < 70:
            entry["label"] = "\ud63c\uc7a1"
            entry["color"] = "#f97316"
        else:
            entry["label"] = "\ub9e4\uc6b0\ud63c\uc7a1"
            entry["color"] = "#ef4444"


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

    # ── Override forecast/trend with predictions (sanitized) ──
    # Uses a sanity check: predicted level cannot deviate more than 20 points
    # from the original heuristic baseline to prevent unrealistic values.
    # Priority 1: Chart predictions from today's live data (same-day expiry, most relevant)
    predictions_to_use = None
    if _CHART_PREDICTIONS and _CHART_PREDICTIONS_DATE == now.strftime("%Y-%m-%d"):
        _apply_levels(forecast, _CHART_PREDICTIONS)
        _apply_levels(trend, _CHART_PREDICTIONS)
        predictions_to_use = _CHART_PREDICTIONS
    # Priority 2: Default predictions from known historical patterns (always available)
    elif _HISTORICAL_PREDICTIONS and _HISTORICAL_DATE:
        _apply_levels(forecast, _HISTORICAL_PREDICTIONS)
        _apply_levels(trend, _HISTORICAL_PREDICTIONS)
        predictions_to_use = _HISTORICAL_PREDICTIONS

    # ── Also apply predictions to the current gauge value when heuristic ──
    # When live data is unavailable, the current level comes from raw heuristic.
    # Predictions (calibrated from known patterns) are more accurate, so we
    # override the current value with the prediction for this hour.
    # The same ±20 max_delta clamp is applied to prevent unrealistic swings.
    if current["data_source"] == "heuristic" and not is_closed and predictions_to_use:
        current_hour = now.hour
        if current_hour in predictions_to_use:
            orig = current["level"]
            pred = predictions_to_use[current_hour]
            clamped = max(orig - 20, min(pred, orig + 20))
            clamped = max(0, min(clamped, 100))
            current["level"] = clamped
            if clamped < 30:
                current["label"] = "여유"
                current["color"] = "#22c55e"
            elif clamped < 50:
                current["label"] = "보통"
                current["color"] = "#eab308"
            elif clamped < 70:
                current["label"] = "혼잡"
                current["color"] = "#f97316"
            else:
                current["label"] = "매우혼잡"
                current["color"] = "#ef4444"

    return {
        "current": current,
        "forecast": forecast,
        "trend": trend,
        "pool": POOL_INFO,
        "time": now.strftime("%Y-%m-%d %H:%M"),
    }


@app.get("/api/health")
async def health_check():
    """Health check endpoint for Vercel monitoring."""
    now = _now_kst()
    live = _LIVE_CACHE is not None
    chart = _CHART_PREDICTIONS_DATE == now.strftime("%Y-%m-%d")
    return {
        "status": "healthy",
        "timestamp": now.isoformat(),
        "data_sources": {
            "live_cache": live,
            "chart_predictions_today": chart,
            "historical_predictions": bool(_HISTORICAL_PREDICTIONS),
        },
        "version": "0.1.0",
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
    return HTMLResponse(
        content=HTML_PAGE,
        headers={
            "Cache-Control": "public, s-maxage=60, stale-while-revalidate=30",
        },
    )


HTML_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1.0, user-scalable=no">
<title>라온체육센터 수영장 혼잡도</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E🏊%3C/text%3E%3C/svg%3E">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #040a18;
    --bg2: #081226;
    --card: rgba(12,24,48,.75);
    --card2: rgba(12,24,48,.55);
    --border: rgba(56,189,248,.08);
    --text: #e2e8f0;
    --dim: #7e93b0;
    --bright: #f8fafc;
    --accent: #38bdf8;
    --radius: 18px;
    --sm: 12px;
    --xs: 8px;
    --fast: .25s cubic-bezier(.22,1,.36,1);
  }
  html { scroll-behavior: smooth; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.5;
    overflow-x: hidden;
    -webkit-font-smoothing: antialiased;
  }
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    z-index: -1;
    background: radial-gradient(ellipse 100% 60% at 50% -20%, rgba(56,189,248,.06) 0%, transparent 100%),
                radial-gradient(ellipse 80% 50% at 20% 100%, rgba(99,102,241,.03) 0%, transparent 100%),
                linear-gradient(180deg, var(--bg) 0%, var(--bg2) 60%, rgba(12,24,48,.8) 100%);
  }
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-thumb { background: rgba(56,189,248,.12); border-radius: 4px; }

  .container {
    max-width: 480px; margin: 0 auto;
    padding: 14px max(14px, env(safe-area-inset-left, 14px)) 32px max(14px, env(safe-area-inset-right, 14px));
  }

  /* ── Header ──────────────────────────────────── */
  header { text-align: center; padding: 18px 0 4px; }
  header h1 {
    font-size: 1.2rem; font-weight: 800;
    color: var(--bright); letter-spacing: -.5px;
  }
  .accent-text { color: var(--accent); }
  header p { font-size: .72rem; color: var(--dim); margin-top: 2px; }

  /* ── Time bar ────────────────────────────────── */
  .time-bar {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    margin: 12px auto 16px;
    padding: 8px 18px;
    background: rgba(12,24,48,.3);
    border: 1px solid var(--border);
    border-radius: 30px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    width: 100%;
  }
  .time-bar .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: #22c55e;
    animation: pulse 2s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes pulse {
    0%,100%{opacity:1;transform:scale(1);box-shadow:0 0 8px rgba(34,197,94,.4)}
    50%{opacity:.4;transform:scale(1.4);box-shadow:0 0 14px rgba(34,197,94,.2)}
  }
  #clock-display {
    font-variant-numeric: tabular-nums;
    font-weight: 700; font-size: .9rem;
    color: var(--bright); letter-spacing: .3px;
  }
  #date-display { font-size: .7rem; color: var(--dim); }
  .day-badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 10px; border-radius: 12px;
    font-size: .65rem; font-weight: 700;
    letter-spacing: .2px;
  }
  .day-badge.weekday { background: rgba(34,197,94,.08); color: #4ade80; border: 1px solid rgba(34,197,94,.1); }
  .day-badge.weekend { background: rgba(239,68,68,.08); color: #f87171; border: 1px solid rgba(239,68,68,.1); }

  /* ── Closure banner ─────────────────────────── */
  .closed-banner { display: none; margin-bottom: 14px; padding: 14px; border-radius: var(--sm); text-align: center; background: rgba(239,68,68,.06); border: 1px solid rgba(239,68,68,.1); }
  .closed-banner .cb-icon { font-size: 1.8rem; display: block; margin-bottom: 4px; }
  .closed-banner .cb-title { font-size: 1rem; font-weight: 800; color: #f87171; }
  .closed-banner .cb-desc { font-size: .78rem; color: rgba(248,113,113,.6); }
  .closed-banner .cb-closure { margin-top: 6px; display: inline-block; padding: 6px 14px; border-radius: 14px; background: rgba(239,68,68,.06); font-size: .72rem; color: #f87171; font-weight: 600; }

  /* ── Gauge card (compact) ───────────────────── */
  .card {
    background: var(--card);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    position: relative; overflow: hidden;
  }
  .gauge-card { padding: 20px 18px 16px; text-align: center; }
  .gauge-card::after {
    content: ''; position: absolute; inset: 0;
    background: radial-gradient(circle at 50% 0%, rgba(56,189,248,.04) 0%, transparent 60%);
    pointer-events: none;
  }

  .gauge-ring {
    width: 150px; height: 150px;
    margin: 0 auto; position: relative;
  }
  .gauge-ring svg { transform: rotate(-90deg); display: block; }
  .gauge-ring .bg-c { fill: none; stroke: rgba(30,50,80,.4); stroke-width: 8; }
  .gauge-ring .fg-c {
    fill: none; stroke-width: 8; stroke-linecap: round;
    transition: stroke-dashoffset 1.2s cubic-bezier(.34,1.56,.64,1), stroke .4s;
  }
  .gauge-label {
    position: absolute; inset: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
  }
  .gauge-label .pct { font-size: 2.3rem; font-weight: 900; line-height: 1; letter-spacing: -1px; transition: color .4s; }
  .gauge-label .lbl { font-size: .85rem; font-weight: 600; margin-top: 4px; transition: color .4s; opacity: .9; }

  /* ── Status badge ───────────────────────────── */
  .g-status {
    position: absolute; top: 12px; right: 14px;
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 12px; border-radius: 14px;
    font-size: .68rem; font-weight: 600;
    z-index: 2;
  }
  .g-status .sd { width: 6px; height: 6px; border-radius: 50%; }
  .g-status.open { background: rgba(34,197,94,.08); color: #4ade80; border: 1px solid rgba(34,197,94,.1); }
  .g-status.open .sd { background: #4ade80; animation: pulse 1.5s infinite; }
  .g-status.break { background: rgba(250,204,21,.08); color: #facc15; border: 1px solid rgba(250,204,21,.1); }
  .g-status.break .sd { background: #facc15; animation: pulse .6s infinite; }
  .g-status.closed { background: rgba(148,163,184,.06); color: #94a3b8; border: 1px solid rgba(148,163,184,.06); }
  .g-status.closed .sd { background: #94a3b8; animation: none; }
  .g-status.closed-day { background: rgba(239,68,68,.08); color: #f87171; border: 1px solid rgba(239,68,68,.1); }
  .g-status.closed-day .sd { background: #f87171; animation: none; }

  /* ── Source badge ───────────────────────────── */
  .src-badge {
    position: absolute; bottom: 12px; left: 16px;
    display: inline-flex; align-items: center; gap: 4px;
    padding: 3px 10px; border-radius: 10px;
    font-size: .6rem; font-weight: 600; z-index: 2;
  }
  .src-badge.live { background: rgba(56,189,248,.08); color: #38bdf8; border: 1px solid rgba(56,189,248,.08); }
  .src-badge.live::before { content: ''; width: 5px; height: 5px; border-radius: 50%; background: #38bdf8; animation: pulse 1.5s infinite; }
  .src-badge.heuristic { background: rgba(148,163,184,.04); color: #94a3b8; border: 1px solid rgba(148,163,184,.04); }
  .src-badge.heuristic::before { content: '📊'; font-size: .5rem; }

  .last-upd {
    position: absolute; bottom: 12px; right: 16px;
    font-size: .55rem; color: rgba(126,147,176,.35); z-index: 2;
  }
  .last-upd.live { color: rgba(56,189,248,.4); }

  /* ── Gender (compact inline) ────────────────── */
  .gender-wrap {
    display: flex; gap: 16px;
    margin: 16px auto 0;
    padding: 12px 16px;
    border-radius: var(--sm);
    background: rgba(0,0,0,.15);
    border: 1px solid rgba(148,163,184,.03);
  }
  .gender-row { flex: 1; display: flex; align-items: center; gap: 6px; }
  .gender-icon { font-size: .85rem; font-weight: 700; }
  .gender-track {
    flex: 1; height: 8px;
    background: rgba(148,163,184,.05);
    border-radius: 6px; overflow: hidden;
  }
  .gender-fill {
    height: 100%; border-radius: 6px;
    transition: width 1s cubic-bezier(.34,1.56,.64,1);
  }
  .gender-fill.m { background: linear-gradient(90deg,#3b82f6,#60a5fa); }
  .gender-fill.f { background: linear-gradient(90deg,#ec4899,#f472b6); }
  .gender-val { font-size: .75rem; font-weight: 700; min-width: 34px; text-align: right; font-variant-numeric: tabular-nums; }
  .gender-val.mv { color: #60a5fa; }
  .gender-val.fv { color: #f472b6; }

  /* ── Tip (compact) ──────────────────────────── */
  .tip {
    margin-top: 14px; padding: 12px 16px;
    border-radius: var(--sm);
    background: rgba(56,189,248,.03);
    font-size: .78rem; color: var(--dim);
    border-left: 2px solid var(--accent);
    text-align: left; line-height: 1.5;
    transition: border-color .4s, background .4s;
  }

  /* ── Operating hours (compact single row) ───── */
  .hours-card { margin-top: 14px; padding: 10px 16px; display: flex; align-items: center; gap: 8px; }
  .hours-card .h-row { flex: 1; display: flex; align-items: center; gap: 4px; }
  .hours-card .h-lbl { font-size: .6rem; font-weight: 600; color: var(--dim); white-space: nowrap; }
  .hours-card .h-val { font-size: .68rem; font-weight: 500; color: var(--bright); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .hours-card .h-div { width: 1px; height: 18px; background: rgba(56,189,248,.1); flex-shrink: 0; }

  /* ── Section title ──────────────────────────── */
  .s-title {
    font-size: .82rem; font-weight: 700;
    color: var(--bright); margin-bottom: 12px;
    display: flex; align-items: center; gap: 6px;
  }
  .s-title .badge {
    font-size: .6rem; font-weight: 600;
    background: rgba(56,189,248,.06);
    padding: 2px 10px; border-radius: 14px;
    color: var(--accent); margin-left: auto;
  }

  /* ── Weekly strip (ultra-compact) ───────────── */
  .weekly-wrap { margin: 20px 0; }
  .weekly-card { padding: 14px 12px 12px; }
  .weekly-strip { display: flex; gap: 6px; justify-content: center; }
  .w-col {
    flex: 1; display: flex; flex-direction: column; align-items: center;
    padding: 8px 2px 8px;
    background: rgba(0,0,0,.12);
    border: 1px solid rgba(56,189,248,.03);
    border-radius: var(--xs);
    transition: background .2s, border-color .2s;
  }
  .w-col.today { background: rgba(56,189,248,.05); border-color: rgba(56,189,248,.15); }
  .w-col.closed { border-color: rgba(239,68,68,.06); background: rgba(239,68,68,.02); }
  .w-col .wt { font-size: .42rem; font-weight: 800; background: linear-gradient(135deg,var(--accent),#22d3ee); color: var(--bg); padding: 1px 6px; border-radius: 6px; margin-bottom: 2px; }
  .w-col .wd { font-size: .68rem; font-weight: 700; color: var(--dim); }
  .w-col.today .wd { color: var(--accent); }
  .w-col.closed .wd { color: #f87171; }
  .w-col .wdt { font-size: .62rem; color: rgba(126,147,176,.5); font-weight: 600; }
  .w-col .wh { font-size: .6rem; font-weight: 600; color: var(--text); margin: 3px 0; font-variant-numeric: tabular-nums; }
  .w-col.today .wh { color: var(--bright); }
  .w-col.closed .wh { color: rgba(248,113,113,.4); }
  .w-col .ws {
    display: inline-flex; align-items: center; gap: 3px;
    padding: 2px 8px; border-radius: 8px;
    font-size: .55rem; font-weight: 700;
  }
  .w-col .ws.open { background: rgba(34,197,94,.06); color: #4ade80; border: 1px solid rgba(34,197,94,.08); }
  .w-col .ws.open::before { content: ''; width: 4px; height: 4px; border-radius: 50%; background: #4ade80; }
  .w-col .ws.cl { background: rgba(239,68,68,.06); color: #f87171; border: 1px solid rgba(239,68,68,.08); }
  .w-col .ws.cl::before { content: '✕'; font-size: .45rem; font-weight: 800; }

  /* ── Forecast ───────────────────────────────── */
  .forecast-section { margin-top: 20px; }
  .forecast-scroll { margin: 0 -14px; }
  .forecast-scroll .f-grid {
    display: flex; overflow-x: auto;
    scroll-snap-type: x mandatory;
    -webkit-overflow-scrolling: touch;
    gap: 8px; padding: 4px 14px 8px;
    scrollbar-width: thin;
    overscroll-behavior: contain;
  }
  .f-item {
    scroll-snap-align: start;
    min-width: 62px; padding: 8px 3px 8px;
    background: var(--card2);
    border: 1px solid var(--border);
    border-radius: var(--xs);
    text-align: center;
    flex-shrink: 0;
    transition: background .2s, transform .2s;
  }
  .f-item:active { transform: scale(.95); }
  .f-item .h { font-size: .65rem; color: var(--dim); margin-bottom: 4px; font-weight: 500; }
  .f-item .bw { height: 36px; display: flex; align-items: flex-end; justify-content: center; margin-bottom: 4px; overflow: hidden; }
  .f-item .bar { width: 14px; border-radius: 3px 3px 1px 1px; min-height: 2px; transition: height .6s cubic-bezier(.34,1.56,.64,1); }
  .f-item .fl { font-size: .65rem; font-weight: 700; }
  .scroll-hint { display: none; text-align: center; font-size: .6rem; color: rgba(126,147,176,.2); margin-top: 2px; letter-spacing: 1px; }
  @media (max-width:640px) {
    .scroll-hint { display: block; }
    .forecast-scroll .f-grid::-webkit-scrollbar { height: 3px; }
    .forecast-scroll .f-grid::-webkit-scrollbar-thumb { background: rgba(56,189,248,.1); border-radius: 3px; }
  }

  /* ── Footer ─────────────────────────────────── */
  footer { text-align: center; margin-top: 32px; padding: 20px 0 8px; border-top: 1px solid rgba(56,189,248,.03); font-size: .7rem; color: var(--dim); line-height: 1.7; }
  footer a { color: var(--accent); text-decoration: none; }
  footer a:active { opacity: .6; }

  /* ── Loading / Error ────────────────────────── */
  .loading { text-align: center; padding: 80px 16px; color: var(--dim); }
  .loading .spinner { width: 36px; height: 36px; border: 3px solid rgba(30,50,80,.3); border-top-color: var(--accent); border-radius: 50%; margin: 0 auto 16px; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading .sp-txt { font-size: .82rem; color: rgba(126,147,176,.5); }

  .error-state { text-align: center; padding: 80px 16px; }
  .error-state .icon { font-size: 2.5rem; display: block; margin-bottom: 12px; }
  .error-state .msg { color: #f87171; font-size: .85rem; }
  .error-state .msg small { opacity: .5; font-size: .75rem; }
  .error-state button {
    margin-top: 20px; padding: 10px 24px;
    border: 1px solid rgba(248,113,113,.15);
    background: rgba(248,113,113,.04);
    color: #f87171;
    border-radius: 10px;
    cursor: pointer;
    font-size: .82rem; font-weight: 600;
    font-family: inherit;
    transition: background .2s;
    -webkit-tap-highlight-color: transparent;
  }
  .error-state button:active { background: rgba(248,113,113,.1); }

  /* ── Mobile touch optimizations ─────────────── */
  @media (hover:none) and (pointer:coarse) {
    .f-item, .w-col, button { cursor: default; -webkit-tap-highlight-color: transparent; touch-action: manipulation; }
  }

  /* ── Very small screens ─────────────────────── */
  @media (max-width:380px) {
    .container { padding: 10px max(10px, env(safe-area-inset-left,10px)) 24px max(10px, env(safe-area-inset-right,10px)); }
    header h1 { font-size: 1rem; }
    header p { font-size: .65rem; }
    .gauge-ring { width: 120px; height: 120px; }
    .gauge-ring svg { width: 120px; height: 120px; }
    .gauge-label .pct { font-size: 1.8rem; }
    .gauge-label .lbl { font-size: .75rem; }
    .gauge-card { padding: 16px 14px 14px; }
    .time-bar { gap: 6px; padding: 6px 14px; font-size: .82rem; }
    #clock-display { font-size: .82rem; }
    .gender-wrap { gap: 8px; padding: 10px 12px; }
    .gender-val { font-size: .68rem; min-width: 28px; }
    .tip { font-size: .72rem; padding: 10px 14px; }
    .hours-card { padding: 8px 12px; }
    .hours-card .h-val { font-size: .62rem; }
    .w-col .wd { font-size: .6rem; }
    .w-col .wh { font-size: .55rem; }
    .w-col { padding: 6px 2px 6px; }
    .weekly-strip { gap: 4px; }
    .weekly-card { padding: 10px 8px 10px; }
    .f-item { min-width: 54px; padding: 6px 2px 6px; }
    .f-item .h { font-size: .58rem; }
    .f-item .fl { font-size: .58rem; }
    .f-item .bw { height: 30px; }
    .f-item .bar { width: 12px; }
    .s-title { font-size: .78rem; margin-bottom: 10px; }
    footer { font-size: .65rem; margin-top: 24px; padding: 16px 0 6px; }
    .g-status { top: 8px; right: 10px; padding: 3px 10px; font-size: .6rem; }
    .src-badge { bottom: 8px; left: 12px; font-size: .55rem; padding: 2px 8px; }
  }

  /* ── No Noto Sans KR needed ────────────────── */
</style>
</head>
<body>

<div class="container" id="app">
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div class="sp-txt">혼잡도 정보를 불러오는 중...</div>
  </div>
  <div class="error-state" id="error" style="display:none">
    <span class="icon">⚠️</span>
    <div class="msg">데이터를 불러오지 못했습니다.<br><small id="error-msg"></small></div>
    <button onclick="fetchData()">다시 시도</button>
  </div>

  <div id="content" style="display:none">
    <header>
      <h1>🏊 <span class="accent-text">라온</span> 수영장 혼잡도</h1>
      <p>전주 라온체육센터 · 25m 6레인</p>
    </header>

    <div class="time-bar">
      <span class="dot"></span>
      <span id="date-display"></span>
      <span id="clock-display">--:--:--</span>
      <span class="day-badge" id="day-badge">--</span>
    </div>

    <div class="closed-banner" id="closed-banner">
      <span class="cb-icon">🔒</span>
      <div class="cb-title">오늘은 휴장일입니다</div>
      <div class="cb-desc" id="closed-desc"></div>
      <div class="cb-closure" id="closed-reason">--</div>
    </div>

    <div class="card gauge-card" id="gauge-card">
      <div class="g-status" id="status-badge">
        <span class="sd"></span>
        <span id="status-text">--</span>
      </div>
      <div class="gauge-ring">
        <svg viewBox="0 0 120 120" width="150" height="150">
          <circle class="bg-c" cx="60" cy="60" r="50"/>
          <circle class="fg-c" id="gauge-circle" cx="60" cy="60" r="50" stroke="#22c55e" stroke-dasharray="314.16" stroke-dashoffset="0"/>
        </svg>
        <div class="gauge-label">
          <div class="pct" id="level-pct">--%</div>
          <div class="lbl" id="level-label">--</div>
        </div>
      </div>
      <div class="src-badge heuristic" id="source-badge">예측</div>
      <div class="last-upd" id="last-updated" style="display:none"></div>

      <div class="gender-wrap">
        <div class="gender-row">
          <span class="gender-icon">♂</span>
          <div class="gender-track"><div class="gender-fill m" id="male-bar" style="width:0%"></div></div>
          <span class="gender-val mv" id="male-rate">0%</span>
        </div>
        <div class="gender-row">
          <span class="gender-icon">♀</span>
          <div class="gender-track"><div class="gender-fill f" id="female-bar" style="width:0%"></div></div>
          <span class="gender-val fv" id="female-rate">0%</span>
        </div>
      </div>

      <div class="tip" id="tip">팁이 로딩 중입니다...</div>
    </div>

    <div class="card hours-card">
      <div class="h-row">
        <span class="h-lbl">🕐</span>
        <span class="h-val" id="weekday-hours">--</span>
      </div>
      <div class="h-div"></div>
      <div class="h-row">
        <span class="h-lbl">🕐</span>
        <span class="h-val" id="weekend-hours">--</span>
      </div>
    </div>

    <div class="weekly-wrap">
      <div class="s-title">📅 주간 운영 현황</div>
      <div class="card weekly-card" id="weekly-card"></div>
    </div>

    <div class="forecast-section">
      <div class="s-title">📊 시간대별 예상 혼잡도 <span class="badge" id="forecast-count">13시간</span></div>
      <div class="forecast-scroll">
        <div class="f-grid" id="forecast-grid"></div>
        <div class="scroll-hint">← 좌우로 스크롤 →</div>
      </div>
    </div>

    <footer>
      <p>⏱ <strong id="footer-source">실시간</strong> · <a href="https://www.jjss.or.kr/reserv/index.9is?contentUid=5232d76d8f95414801904883b431549b&amp;searchType=PL004&amp;subPath=" target="_blank">실시간 현황</a> 반영</p>
      <p><a href="https://map.naver.com/p/search/%EC%A0%84%EC%A3%BC%20%EB%9D%BC%EC%98%A8%EC%B2%B4%EC%9C%A1%EC%84%BC%ED%84%B0/place/1181720929" target="_blank">네이버 지도</a> · 전주시설관리공단</p>
    </footer>
  </div>
</div>

<script>
const CIRCUMFERENCE = Math.PI * 100;
function setGauge(pct,color){
  const c=document.getElementById('gauge-circle');
  c.style.strokeDashoffset=CIRCUMFERENCE-(pct/100)*CIRCUMFERENCE;
  c.style.stroke=color;
  document.getElementById('tip').style.borderColor=color;
  document.getElementById('tip').style.background=color+'08';
}
function animateNumber(el,target,suffix,color){
  const d=800,start=performance.now(),sv=parseInt(el.textContent)||0;
  function tick(now){
    const p=Math.min((now-start)/d,1),e=p<.5?4*p*p*p:1-Math.pow(-2*p+2,3)/2;
    el.textContent=Math.round(sv+(target-sv)*e)+suffix;
    el.style.color=color;
    if(p<1)requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
function animateBar(el,target){
  const d=800,start=performance.now(),sv=parseFloat(el.style.width)||0;
  function tick(now){
    const p=Math.min((now-start)/d,1),e=p<.5?4*p*p*p:1-Math.pow(-2*p+2,3)/2;
    el.style.width=(sv+(target-sv)*e)+'%';
    if(p<1)requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
function getKST(){const n=new Date();return new Date(n.getTime()+n.getTimezoneOffset()*6e4+324e5)}
function updateClock(){
  const n=getKST();
  document.getElementById('clock-display').textContent=
    String(n.getHours()).padStart(2,'0')+':'+String(n.getMinutes()).padStart(2,'0')+':'+String(n.getSeconds()).padStart(2,'0');
}
const DAY_KR=['일','월','화','수','목','금','토'];
function updateDateDisplay(s){
  const el=document.getElementById('date-display');
  if(s){const d=new Date(s.replace(' ','T')+'+09:00');if(!isNaN(d)){el.textContent=d.getFullYear()+'.'+String(d.getMonth()+1).padStart(2,'0')+'.'+String(d.getDate()).padStart(2,'0')+' ('+DAY_KR[d.getDay()]+')';return}}
  const d=getKST();
  el.textContent=d.getFullYear()+'.'+String(d.getMonth()+1).padStart(2,'0')+'.'+String(d.getDate()).padStart(2,'0')+' ('+DAY_KR[d.getDay()]+')';
}
function setStatusBadge(st){
  const b=document.getElementById('status-badge'),t=document.getElementById('status-text');
  t.textContent=st;b.className='g-status';
  if(st==='운영중')b.classList.add('open');
  else if(st==='수질정화시간')b.classList.add('break');
  else if(st==='휴장')b.classList.add('closed-day');
  else b.classList.add('closed');
}
function setGenderRates(m,f){
  document.getElementById('male-rate').textContent=m+'%';document.getElementById('female-rate').textContent=f+'%';
  animateBar(document.getElementById('male-bar'),Math.min(m,100));
  animateBar(document.getElementById('female-bar'),Math.min(f,100));
}
function renderForecast(forecast){
  const grid=document.getElementById('forecast-grid');grid.innerHTML='';
  forecast.forEach((f,i)=>{
    const div=document.createElement('div');div.className='f-item';
    const barBg=f.color+'cc';
    div.innerHTML='<div class="h">'+f.hour+'</div><div class="bw"><div class="bar" style="height:2px;background:'+barBg+'"></div></div><div class="fl" style="color:'+f.color+'">'+f.level+'%</div>';
    grid.appendChild(div);
    requestAnimationFrame(()=>{const b=div.querySelector('.bar');if(b)b.style.height=Math.min(f.level*.36+2,38)+'px'});
  });
  document.getElementById('forecast-count').textContent=forecast.length+'시간';
}
function renderWeeklySchedule(schedule){
  const card=document.getElementById('weekly-card');
  const strip=document.createElement('div');strip.className='weekly-strip';
  schedule.forEach((d,i)=>{
    const col=document.createElement('div');col.className='w-col';
    if(d.is_today)col.classList.add('today');
    if(d.is_closed)col.classList.add('closed');
    const ch=d.is_closed?'--:--':d.hours.replace(/:00/g,'');
    col.innerHTML=
      (d.is_today?'<div class="wt">TODAY</div>':'')+
      '<div class="wd">'+d.day_name+'</div>'+
      '<div class="wdt">'+d.date+'</div>'+
      '<div class="wh">'+(d.is_closed?'--:--':ch)+'</div>'+
      '<div class="ws '+(d.is_closed?'cl':'open')+'">'+(d.is_closed?'휴장':'운영')+'</div>';
    strip.appendChild(col);
  });
  card.innerHTML='';card.appendChild(strip);
}
async function fetchWeeklySchedule(){
  try{const r=await fetch('/api/weekly-schedule');if(!r.ok)throw Error('HTTP '+r.status);const d=await r.json();renderWeeklySchedule(d.schedule);}
  catch(e){console.warn('Schedule failed:',e.message);}
}
document.addEventListener('DOMContentLoaded',()=>{
  const w=document.querySelector('.forecast-scroll');if(!w)return;
  const g=w.querySelector('.f-grid'),h=w.querySelector('.scroll-hint');
  if(!g||!h)return;
  g.addEventListener('scroll',()=>{h.style.opacity='0';h.style.pointerEvents='none';},{once:true});
});
async function fetchData(){
  try{
    const r=await fetch('/api/congestion');if(!r.ok)throw Error('HTTP '+r.status);
    const d=await r.json();
    document.getElementById('loading').style.display='none';
    document.getElementById('error').style.display='none';
    const content=document.getElementById('content');
    const firstLoad=content.style.display==='none';
    content.style.display='block';

    updateDateDisplay(d.time);
    document.getElementById('clock-display').textContent=d.time.split(' ')[1]+':00';

    const badge=document.getElementById('day-badge');
    if(d.current.is_weekend){badge.textContent='주말';badge.className='day-badge weekend';}
    else{badge.textContent='평일';badge.className='day-badge weekday';}

    const cl=document.getElementById('closed-banner'),cd=document.getElementById('closed-desc'),cr=document.getElementById('closed-reason');
    if(d.current.is_closed){
      cl.style.display='block';
      cr.textContent=d.current.closed_reason||'정기휴장';
      cd.textContent='오늘은 수영장 휴장일입니다. 운영 시간에 방문해주세요.';
      document.getElementById('level-pct').textContent='휴장';
      document.getElementById('level-pct').style.color='#ef4444';
      document.getElementById('level-label').textContent='오늘은 쉬는 날';
      document.getElementById('level-label').style.color='#f87171';
      document.querySelector('.gender-wrap').style.display='none';
      document.getElementById('tip').style.display='none';
      document.getElementById('source-badge').style.display='none';
    } else {
      cl.style.display='none';
      document.querySelector('.gender-wrap').style.display='';
      document.getElementById('tip').style.display='';
      document.getElementById('source-badge').style.display='';
    }

    setStatusBadge(d.current.status);

    const sb=document.getElementById('source-badge'),fs=document.getElementById('footer-source'),lu=document.getElementById('last-updated');
    if(d.current.data_source==='live'){
      sb.className='src-badge live';sb.textContent='실시간';fs.textContent='실시간';
      if(d.current.last_updated){
        const n=getKST(),u=new Date(d.current.last_updated.replace(' ','T')+'+09:00'),ds=Math.floor((n-u)/1000);
        lu.textContent='⏱ '+(ds<60?'방금 전':(ds<3600?Math.floor(ds/60)+'분 전':Math.floor(ds/3600)+'시간 전'));
        lu.className='last-upd live';lu.style.display='';
      }
    } else {
      sb.className='src-badge heuristic';sb.textContent='예측';fs.textContent='예측';lu.style.display='none';
    }

    setGenderRates(d.current.male_rate,d.current.female_rate);

    if(!d.current.is_closed){
      const c=d.current,pe=document.getElementById('level-pct'),le=document.getElementById('level-label');
      if(firstLoad){pe.textContent='0%';le.textContent=c.label;le.style.color=c.color;animateNumber(pe,c.level,'%',c.color);}
      else{animateNumber(pe,c.level,'%',c.color);le.textContent=c.label;le.style.color=c.color;}
      setGauge(c.level,c.color);
      document.getElementById('tip').innerHTML=c.tip;
    }

    const p=d.pool;
    document.getElementById('weekday-hours').textContent=p.weekday_hours;
    document.getElementById('weekend-hours').textContent=p.weekend_hours;

    renderForecast(d.forecast);
  } catch(e){
    document.getElementById('loading').style.display='none';
    const el=document.getElementById('error');el.style.display='block';
    document.getElementById('error-msg').textContent=e.message;
  }
}
updateClock();setInterval(updateClock,1000);
fetchData();fetchWeeklySchedule();
setInterval(fetchData,60000);
setInterval(fetchWeeklySchedule,300000);
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
