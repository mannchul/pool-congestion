"""전주 라온체육센터 수영장 혼잡도 웹 애플리케이션"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import urllib.request
import re as _re

app = FastAPI(
    title="라온체육센터 수영장 혼잡도",
    description="전주 라온체육센터 수영장의 시간대별 예상 혼잡도를 확인하세요.",
)

# ── Pool information ─────────────────────────────────────────────────────────

POOL_INFO = {
    "name": "전주 라온체육센터 수영장",
    "address": "전북 전주시 덕진구 오공로 43-6",
    "phone": "063-239-2760",
    "weekday_hours": "06:00 ~ 20:00 (브레이크타임 12:00~13:00)",
    "saturday_hours": "06:00 ~ 17:00",
    "sunday_holiday_hours": "10:00 ~ 17:00",
    "weekend_hours": "토요일 06:00~17:00 / 일요일·공휴일 10:00~17:00",
    "break_time": "12:00~13:00 (평일)",
    "closed_days": "매월 첫째·셋째 주 일요일",
    "pool_size": "25m 6레인",
    "features": ["어린이풀", "헬스장", "탁구장"],
}


# ── Real data scraper ──────────────────────────────────────────────────────

_LIVE_CACHE: Dict | None = None
_LIVE_CACHE_TIME: datetime | None = None


def _scrape_live_data() -> Dict | None:
    """Scrape real-time congestion data from jjss.or.kr.

    The page HTML (EUC-KR) contains patterns like:
      <span>23%</span>   (total utilization)
      data-util="man" ... <span>11%</span>   (male)
      data-util="woman" ... <span>30%</span> (female)
      class="... bg_spare" = 여유, bg_general = 보통, bg_congestion = 혼잡

    Returns a dict with 'level', 'male_rate', 'female_rate', 'label'
    and 'scraped_at', or None if scraping fails.
    """
    global _LIVE_CACHE, _LIVE_CACHE_TIME

    now = datetime.now(ZoneInfo("Asia/Seoul"))

    # Cache for up to 60 seconds
    if _LIVE_CACHE is not None and _LIVE_CACHE_TIME is not None:
        if (now - _LIVE_CACHE_TIME).total_seconds() < 60:
            return _LIVE_CACHE

    url = (
        "https://www.jjss.or.kr/reserv/index.9is"
        "?contentUid=5232d76d8f95414801904883b431549b"
        "&searchType=PL004&subPath="
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=8)
        raw = resp.read()
    except Exception:
        _LIVE_CACHE = None
        return None

    # Find all percentage patterns: >XX%< in the entire page
    percentages = _re.findall(rb">(\d+)%<", raw)

    if len(percentages) < 1:
        _LIVE_CACHE = None
        return None

    total_pct = int(percentages[0])

    # Male/female rates: 2nd = male (man), 3rd = female (woman) in page order
    male_pct = int(percentages[1]) if len(percentages) > 1 else total_pct // 2
    female_pct = int(percentages[2]) if len(percentages) > 2 else total_pct // 2

    # Determine label from class name (search the whole page)
    # bg_spare = 여유, bg_general = 보통, bg_congestion = 혼잡
    status_label = "여유"
    congestion_match = _re.search(rb"bg_congestion", raw)
    general_match = _re.search(rb"bg_general", raw)
    spare_match = _re.search(rb"bg_spare", raw)
    if congestion_match:
        status_label = "혼잡"
    elif general_match:
        status_label = "보통"
    elif spare_match:
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
    return result


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
      - status: "운영중" / "브레이크타임" / "운영종료"
    """
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
        day_type = "평일"
        if 6 <= hour < 8:
            level, label, tip = 30, "여유", "아침 수영하기 좋은 시간입니다. 한적하게 운동하세요!"
            male_rate, female_rate, status = 35, 25, "운영중"
        elif 8 <= hour < 11:
            level, label, tip = 20, "여유", "오전 중 가장 한적한 시간대입니다."
            male_rate, female_rate, status = 15, 25, "운영중"
        elif 11 <= hour < 12:
            level, label, tip = 40, "여유", "점심 전, 아직은 여유로운 편입니다."
            male_rate, female_rate, status = 35, 45, "운영중"
        elif 12 <= hour < 13:
            level, label, tip = 0, "브레이크타임", "⚠️ 브레이크타임(12:00~13:00)입니다. 시설 정비 시간이니 참고하세요."
            male_rate, female_rate, status = 0, 0, "브레이크타임"
        elif 13 <= hour < 15:
            level, label, tip = 35, "여유", "오후 시간대는 비교적 여유롭습니다."
            male_rate, female_rate, status = 30, 40, "운영중"
        elif 15 <= hour < 16:
            level, label, tip = 25, "여유", "오후 늦게, 가장 한적한 시간 중 하나입니다."
            male_rate, female_rate, status = 20, 30, "운영중"
        elif 16 <= hour < 18:
            level, label, tip = 60, "보통", "퇴근 시간대 방문객이 증가합니다."
            male_rate, female_rate, status = 65, 55, "운영중"
        elif 18 <= hour <= 20:
            level, label, tip = 80, "혼잡", "저녁 피크 시간입니다. 2시간 정도 여유를 두고 방문하세요."
            male_rate, female_rate, status = 85, 75, "운영중"
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


def _get_operating_hours(now: datetime) -> tuple[int, int]:
    """Return (start_hour, end_hour) for the given day based on operating hours.

    Returns None for end_hour if the day is closed.
    """
    weekday = now.weekday()
    if weekday == 6:
        return (10, 17)  # Sunday: 10:00~17:00
    elif weekday == 5:
        return (6, 17)   # Saturday: 06:00~17:00
    else:
        return (6, 20)   # Weekday: 06:00~20:00


def _hourly_forecast(now: datetime) -> List[Dict]:
    """Generate congestion forecast for operating hours only."""
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
        if data["status"] == "브레이크타임":
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

    # Try to get live data
    live = _scrape_live_data()

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

    forecast = _hourly_forecast(now)
    return {
        "current": current,
        "forecast": forecast,
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
    .container { padding-left: max(20px, env(safe-area-inset-left)); padding-right: max(20px, env(safe-area-inset-right)); }
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
    max-width: 920px;
    margin: 0 auto;
    padding: 20px 20px 50px;
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
    padding: 28px 0 6px;
    position: relative;
  }
  header::after {
    content: '';
    display: block;
    width: 60px;
    height: 3px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    border-radius: 4px;
    margin: 14px auto 0;
    opacity: 0.4;
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
    gap: 14px;
    margin: 18px 0 28px;
    padding: 12px 24px;
    background: rgba(12, 24, 48, 0.4);
    border: 1px solid rgba(56, 189, 248, 0.06);
    border-radius: 50px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    width: fit-content;
    margin-left: auto;
    margin-right: auto;
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
    top: 16px;
    right: 20px;
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

  /* ── Gender rates ───────────────────────────────────── */
  .gender-rates {
    margin: 20px auto 0;
    max-width: 340px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 22px;
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
    margin-bottom: 16px;
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
    padding: 40px 30px 32px;
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
    bottom: 16px;
    left: 22px;
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
    margin-top: 18px;
    padding: 16px 22px;
    background: rgba(56, 189, 248, 0.04);
    border-radius: var(--radius-sm);
    font-size: 0.88rem;
    color: var(--text-dim);
    border-left: 3px solid var(--accent);
    text-align: left;
    transition: border-color 0.6s, background 0.6s;
    line-height: 1.55;
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
    gap: 14px;
    margin: 28px 0;
  }
  .info-item {
    background: var(--card);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    border-radius: var(--radius-sm);
    padding: 18px 22px;
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

  /* ── Trend chart ─────────────────────────────────────── */
  .trend-card {
    padding: 20px 16px 12px;
    overflow: hidden;
  }
  .trend-card canvas {
    width: 100%;
    height: auto;
    display: block;
    border-radius: var(--radius-xs);
  }

  /* ── Forecast ─────────────────────────────────────────── */
  .forecast-section { margin-top: 32px; }

  .forecast-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(88px, 1fr));
    gap: 10px;
  }
  .forecast-item {
    background: var(--card);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid var(--card-border);
    border-radius: var(--radius-sm);
    padding: 14px 6px 12px;
    text-align: center;
    transition: transform var(--transition), background var(--transition), box-shadow var(--transition), border-color var(--transition);
    cursor: default;
    animation: scale-in 0.5s var(--bounce) both;
    flex-shrink: 0;
    position: relative;
    overflow: hidden;
  }
  .forecast-item::before {
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 2px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    opacity: 0;
    transition: opacity 0.4s;
  }
  .forecast-item:hover {
    background: var(--card-hover);
    transform: translateY(-5px) scale(1.04);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.25);
    border-color: rgba(56, 189, 248, 0.08);
  }
  .forecast-item:hover::before { opacity: 0.4; }
  .forecast-item .hour {
    font-size: 0.76rem;
    color: var(--text-dim);
    margin-bottom: 10px;
    font-weight: 500;
    letter-spacing: 0.3px;
  }
  .forecast-item .bar-wrap {
    height: 56px;
    display: flex;
    align-items: flex-end;
    justify-content: center;
    margin-bottom: 8px;
  }
  .forecast-item .bar {
    width: 24px;
    border-radius: 6px 6px 3px 3px;
    min-height: 3px;
    transition: height 1s var(--bounce), background 0.3s;
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
    top: -2px; left: -3px;
    right: -3px; height: 6px;
    border-radius: 4px;
    background: inherit;
    filter: blur(4px);
    opacity: 0.6;
    pointer-events: none;
  }
  .forecast-item .f-label {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: -0.2px;
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
    margin-top: 50px;
    padding: 24px 0 10px;
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
    @supports(padding:max(0px)) {
      .container { padding: 14px max(14px, env(safe-area-inset-left)) 36px max(14px, env(safe-area-inset-right)); }
    }
    .container { padding: 14px 14px 36px; }
    header h1 { font-size: 1.4rem; }
    header::after { width: 40px; }
    .time-bar {
      padding: 10px 16px;
      gap: 10px;
      font-size: 0.95rem;
      width: 100%;
      border-radius: 30px;
    }
    #clock-display { font-size: 1rem; }
    #date-display { font-size: 0.75rem; }
    .gauge-card { padding: 28px 18px 24px; }
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
      bottom: 12px;
      left: 16px;
      font-size: 0.58rem;
    }
    .info-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
    .info-item { padding: 16px 18px; min-height: 60px; }
    .info-item .value { font-size: 0.88rem; }
    .gender-rates { padding: 16px 18px; gap: 14px; }
    .gender-bar-track { height: 14px; }
    .gender-row { gap: 10px; min-height: 40px; }
    .section-title { font-size: 0.95rem; }

    /* Forecast: horizontal scroll on mobile */
    .forecast-scroll-wrap { margin: 0 -14px; }
    .forecast-scroll-wrap .forecast-grid {
      display: flex;
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
      gap: 10px;
      padding: 4px 14px 12px;
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
      min-width: 80px;
      padding: 12px 6px 12px;
      min-height: 100px;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    .forecast-scroll-wrap .forecast-item:last-child { scroll-snap-align: end; }
    @media (hover: hover) {
      .forecast-scroll-wrap .forecast-item:hover {
        transform: translateY(-4px) scale(1.06);
      }
    }
    .scroll-hint { display: block; }

    .forecast-item .bar { width: 20px; }
    .forecast-item .bar-wrap { height: 48px; }
    .forecast-item .hour { font-size: 0.72rem; }
    .forecast-item .f-label { font-size: 0.7rem; }
    .forecast-item .bar::before {
      top: -3px; left: -4px;
      right: -4px; height: 8px;
    }
  }

  @media (max-width: 420px) {
    .info-grid { grid-template-columns: 1fr; }
    .gender-rates { max-width: 100%; }
    .time-bar { flex-wrap: wrap; justify-content: center; gap: 6px; padding: 10px 12px; }
    .gauge-ring { width: 150px; height: 150px; }
    .gauge-ring svg { width: 150px; height: 150px; }
    .gauge-label .pct { font-size: 2.2rem; }
    .gauge-label .pct-label { font-size: 0.85rem; }
    .gauge-tip { font-size: 0.82rem; padding: 14px 16px; }
    footer { font-size: 0.72rem; }
  }

  @media (max-width: 360px) {
    @supports(padding:max(0px)) {
      .container { padding: 10px max(10px, env(safe-area-inset-left)) 32px max(10px, env(safe-area-inset-right)); }
    }
    .container { padding: 10px 10px 32px; }
    header h1 { font-size: 1.2rem; }
    header p { font-size: 0.75rem; }
    .time-bar { font-size: 0.85rem; gap: 5px; padding: 8px 10px; }
    #clock-display { font-size: 0.85rem; }
    #date-display { font-size: 0.68rem; }
    .gauge-card { padding: 22px 14px 20px; }
    .gauge-ring { width: 130px; height: 130px; }
    .gauge-ring svg { width: 130px; height: 130px; }
    .gauge-label .pct { font-size: 2rem; }
    .gauge-label .pct-label { font-size: 0.8rem; }
    .gauge-status-badge { top: 10px; right: 10px; padding: 4px 10px; font-size: 0.65rem; min-height: 30px; }
    .source-badge { bottom: 8px; left: 12px; font-size: 0.55rem; padding: 3px 8px; }
    .info-item { padding: 12px 14px; min-height: 50px; }
    .info-item .value { font-size: 0.82rem; }
    .info-item .label { font-size: 0.63rem; }
    .gender-rates { padding: 12px 14px; gap: 10px; }
    .gender-bar-track { height: 12px; }
    .gender-row { gap: 8px; min-height: 34px; }
    .gender-icon { font-size: 0.9rem; }
    .gender-label { font-size: 0.7rem; width: 34px; }
    .gender-value { font-size: 0.75rem; width: 36px; }
    .section-title { font-size: 0.85rem; }
    .forecast-item { min-width: 68px; padding: 8px 4px 10px; min-height: 88px; }
    .forecast-item .hour { font-size: 0.65rem; }
    .forecast-item .bar { width: 16px; }
    .forecast-item .bar-wrap { height: 40px; }
    .forecast-item .f-label { font-size: 0.62rem; }
    .trend-card { padding: 14px 10px 8px; }
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
      <div class="info-item">
        <div class="label">📍 주소</div>
        <div class="value" id="address">--</div>
      </div>
      <div class="info-item">
        <div class="label">📞 전화</div>
        <div class="value" id="phone">--</div>
      </div>
      <div class="info-item">
        <div class="label">🕐 평일 운영</div>
        <div class="value" id="weekday-hours">--</div>
      </div>
      <div class="info-item">
        <div class="label">🕐 주말 운영</div>
        <div class="value" id="weekend-hours">--</div>
      </div>
    </div>

    <!-- Hourly forecast -->
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

    <!-- Daily trend chart -->
    <div class="forecast-section animate-in animate-in-delay-5">
      <div class="section-title">
        <span>📈</span> 오늘 혼잡도 트렌드
        <span class="badge-count" id="trend-count">전체</span>
      </div>
      <div class="trend-card glass-card">
        <canvas id="trend-canvas" width="800" height="220"></canvas>
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
  } else if (status === '브레이크타임') {
    badge.classList.add('break');
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

// ── Trend chart (Canvas) ─────────────────────────────────────────────────────
function renderTrendChart(trend) {
  const canvas = document.getElementById('trend-canvas');
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;

  ctx.clearRect(0, 0, w, h);

  const count = trend.length;
  if (count < 2) return;

  const pad = { top: 20, bottom: 32, left: 42, right: 32 };
  const chartW = w - pad.left - pad.right;
  const chartH = h - pad.top - pad.bottom;
  const maxLevel = 100;

  // Grid lines
  ctx.strokeStyle = 'rgba(56, 189, 248, 0.05)';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  for (let lv = 0; lv <= 100; lv += 25) {
    const y = pad.top + chartH - (lv / maxLevel) * chartH;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(w - pad.right, y);
    ctx.stroke();
    // Y-axis labels
    ctx.fillStyle = 'rgba(126, 147, 176, 0.35)';
    ctx.font = '9px sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(lv + '%', pad.left - 6, y + 3);
  }
  ctx.setLineDash([]);

  // Build path
  const points = trend.map((d, i) => ({
    x: pad.left + (i / (count - 1)) * chartW,
    y: pad.top + chartH - (d.level / maxLevel) * chartH,
    color: d.color,
    level: d.level,
    hour: d.hour,
  }));

  // Fill area with gradient
  ctx.beginPath();
  ctx.moveTo(points[0].x, pad.top + chartH);
  points.forEach(p => ctx.lineTo(p.x, p.y));
  ctx.lineTo(points[points.length - 1].x, pad.top + chartH);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + chartH);
  grad.addColorStop(0, 'rgba(56, 189, 248, 0.15)');
  grad.addColorStop(0.4, 'rgba(56, 189, 248, 0.06)');
  grad.addColorStop(1, 'rgba(56, 189, 248, 0.005)');
  ctx.fillStyle = grad;
  ctx.fill();

  // Draw line segments with per-segment color
  ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  for (let i = 0; i < points.length - 1; i++) {
    const p1 = points[i];
    const p2 = points[i + 1];
    const segColor = p1.level >= p2.level ? p1.color : p2.color;
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.strokeStyle = segColor;
    ctx.lineWidth = 2.5;
    ctx.stroke();
  }

  // Draw dots with glow
  points.forEach(p => {
    // Glow
    ctx.beginPath();
    ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
    ctx.fillStyle = p.color + '20';
    ctx.fill();

    // Main dot
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = p.color;
    ctx.fill();
    ctx.strokeStyle = 'rgba(4, 10, 24, 0.5)';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // X-axis labels
    ctx.fillStyle = 'rgba(126, 147, 176, 0.4)';
    ctx.font = '9px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(p.hour, p.x, h - pad.bottom + 18);
  });

  // High / low markers
  const max = points.reduce((a, b) => a.level > b.level ? a : b);
  const min = points.reduce((a, b) => a.level < b.level ? a : b);

  ctx.fillStyle = 'rgba(248, 113, 113, 0.7)';
  ctx.font = 'bold 9px sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText('▲ 최고 ' + max.level + '% (' + max.hour + ')', max.x + 8, max.y - 4);

  ctx.fillStyle = 'rgba(74, 222, 128, 0.7)';
  ctx.fillText('▼ 최저 ' + min.level + '% (' + min.hour + ')', pad.left, pad.top + chartH - 6);
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
        bar.style.height = `${f.level * 0.48 + 4}px`;
      }
    });
  });

  document.getElementById('forecast-count').textContent = `${forecast.length}시간`;
}

// ── Fetch trend data ─────────────────────────────────────────────────────────
async function fetchTrend() {
  try {
    const res = await fetch('/api/daily-trend');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    document.getElementById('trend-count').textContent = `${data.trend.length}시간`;
    renderTrendChart(data.trend);
  } catch (err) {
    console.warn('Trend chart fetch failed:', err.message);
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

    // Status badge
    setStatusBadge(data.current.status);

    // Data source badge
    const sourceBadge = document.getElementById('source-badge');
    const footerSource = document.getElementById('footer-source');
    if (data.current.data_source === 'live') {
      sourceBadge.className = 'source-badge live';
      sourceBadge.textContent = '실시간';
      footerSource.textContent = '실시간';
    } else {
      sourceBadge.className = 'source-badge heuristic';
      sourceBadge.textContent = '예측';
      footerSource.textContent = '예측';
    }

    // Gender rates
    setGenderRates(data.current.male_rate, data.current.female_rate);

    // Gauge
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

    // Pool info
    const p = data.pool;
    document.getElementById('address').textContent = p.address;
    document.getElementById('phone').textContent = p.phone;
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
fetchTrend();

setInterval(fetchData, 60000);
setInterval(fetchTrend, 120000);
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
