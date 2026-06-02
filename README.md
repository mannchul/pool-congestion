# 🏊 라온체육센터 수영장 혼잡도

전주 라온체육센터 수영장의 실시간 혼잡도와 시간대별 예상 혼잡도를 확인하는 웹 애플리케이션입니다.

## 기술 스택

| 계층 | 기술 |
|------|------|
| **Backend** | Python 3.12, FastAPI |
| **Frontend** | Vanilla JavaScript + CSS (단일 HTML, SPA) |
| **데이터** | jjss.or.kr 라이브 스크래핑 (urllib + regex) |
| **배포** | Vercel (Serverless Functions, @vercel/python) |

## API 엔드포인트

| 경로 | 설명 | 캐시 |
|------|------|------|
| `GET /` | 메인 대시보드 HTML 페이지 | `s-maxage=60` |
| `GET /api/congestion` | 현재 혼잡도 + 시간대별 예보 + 트렌드 | `s-maxage=30` |
| `GET /api/daily-trend` | 오늘 하루 혼잡도 트렌드 데이터 | `s-maxage=30` |
| `GET /api/weekly-schedule` | 7일간 주간 운영 스케줄 | `s-maxage=30` |
| `GET /api/health` | 헬스 체크 (상태, 데이터소스 현황, 버전) | `s-maxage=30` |

### 응답 예시 (`/api/congestion`)

```json
{
  "current": {
    "level": 42,
    "label": "보통",
    "color": "#eab308",
    "tip": "오전 시간, 아침 피크가 지나며 방문객이 점차 감소합니다.",
    "day_type": "평일",
    "is_weekend": false,
    "male_rate": 19,
    "female_rate": 58,
    "status": "운영중",
    "data_source": "live",
    "is_closed": false,
    "closed_reason": null,
    "last_updated": "2026-06-02 09:32:45"
  },
  "forecast": [ /* 시간대별 예보 */ ],
  "trend": [ /* 오늘 전체 트렌드 */ ],
  "pool": { /* 수영장 정보 */ },
  "time": "2026-06-02 09:32"
}
```

## 로컬 개발

### 의존성 설치

```bash
# uv 사용 (권장) — pyproject.toml에서 모든 의존성 설치
uv sync

# 또는 pip 사용 (uvicorn은 수동 설치 필요)
pip install -r requirements.txt
pip install uvicorn  # 로컬 실행에 필요 (Vercel에는 불필요)
```

> `uvicorn`은 `requirements.txt`에 포함되어 있지 않습니다.
> Vercel Python Runtime이 ASGI를 자체 처리하기 때문에 프로덕션에는 불필요하기 때문입니다.
> 로컬 개발 시에는 `main()` 함수가 `uvicorn.run()`을 호출하므로 별도 설치가 필요합니다.

### 개발 서버 실행

```bash
python main.py
```

`http://localhost:8000` 에서 확인할 수 있습니다.

### 테스트 실행

```bash
python -m pytest test_main.py -v
```

총 **210개 테스트**가 실행됩니다 (휴리스틱, 스크래퍼, 예측 함수, 헬퍼, API 통합).

---

## Vercel 배포

### 사전 준비

- [Vercel 계정](https://vercel.com) 생성
- (선택) [Vercel CLI](https://vercel.com/docs/cli) 설치: `npm i -g vercel`

### 배포 전 확인 사항

프로젝트 루트(`pool-congestion/`)에 다음 파일들이 준비되어 있어야 합니다:

```
pool-congestion/
├── main.py              # FastAPI 앱 (app 변수 → Vercel이 ASGI 자동 인식)
├── vercel.json          # Vercel 설정 (빌더, 라우팅, 캐싱 헤더)
├── requirements.txt     # 프로덕션 의존성 (fastapi만 포함)
├── .python-version      # Python 3.12 고정
└── .vercelignore        # 배포 제외 파일 정의
```

> **참고:** `pyproject.toml`, `uv.lock`, `test_main.py` 등은 Vercel 배포에 필요하지 않으며 `.vercelignore`에 의해 자동으로 제외됩니다.

### 방법 1: Vercel CLI (직접 배포)

```bash
# Vercel CLI가 없으면 설치
npm install -g vercel

# 로그인 (최초 1회)
vercel login

# pool-congestion 디렉토리로 이동
cd pool-congestion

# preview 배포 (설정 확인용)
vercel

# 프로덕션 배포
vercel --prod
```

### 방법 2: GitHub 연동 (권장)

1. [Vercel Dashboard](https://vercel.com) → **Add New → Project**
2. GitHub 저장소(`pool-congestion`) 연결
3. **Root Directory**를 `pool-congestion/` 으로 설정
4. **Framework Preset**은 자동으로 `Other` 감지 (별도 설정 불필요)
5. **Deploy** 클릭
6. 배포 완료 후 `https://<project>.vercel.app` 에서 접속 가능

### 배포 설정 파일 설명

#### `vercel.json`

```json
{
  "builds": [
    {
      "src": "main.py",
      "use": "@vercel/python"
    }
  ],
  "routes": [
    {
      "src": "/api/(.*)",
      "dest": "main.py",
      "headers": {
        "Cache-Control": "public, s-maxage=30, stale-while-revalidate=15"
      }
    },
    {
      "src": "/",
      "dest": "main.py",
      "headers": {
        "Cache-Control": "public, s-maxage=60, stale-while-revalidate=30"
      }
    },
    {
      "src": "/(.*)",
      "dest": "main.py"
    }
  ]
}
```

| 설정 | 설명 |
|------|------|
| `builds[0].use` | `@vercel/python` — Python 서버리스 함수 빌더 |
| `routes[0]` | API 라우트 → `s-maxage=30` (CDN에 30초 캐싱), `stale-while-revalidate=15` (15초 동안 기존 캐시 사용 후 백그라운드 갱신) |
| `routes[1]` | 루트 페이지 → `s-maxage=60` (1분 CDN 캐싱) |
| `routes[2]` | 그 외 모든 요청 → 캐싱 없음 (폴백) |

#### `.vercelignore`

```text
__pycache__/
*.py[cod]
test_main.py
.pytest_cache/
.venv/
.gitignore
README.md
uv.lock
```

배포에서 제외할 파일을 정의합니다. 이 파일들 없이도 앱이 정상 작동하며, 배포 크기를 줄이고 빌드 시간을 단축합니다.

#### `requirements.txt`

```
fastapi==0.136.3
```

> `uvicorn`은 **Vercel에 필요하지 않습니다.** Vercel Python Runtime이 자체적으로 ASGI/WSGI 인터페이스를 처리합니다. `uvicorn`은 로컬 개발 시에만 `main.py`의 `if __name__ == "__main__"` 블록에서 사용됩니다.

---

### 캐싱 정책

| 엔드포인트 | CDN 캐시 (`s-maxage`) | Stale-while-revalidate | 이유 |
|-----------|----------------------|----------------------|------|
| `/` | 60초 | 30초 | HTML은 상대적으로 덜 자주 변경 |
| `/api/*` | 30초 | 15초 | 실시간 혼잡도 — 짧은 캐시로 최신성 유지 |
| 기타 | 캐시 없음 | — | 폴백 |

캐싱은 **Vercel Edge Network** 레벨에서 적용되며, 방문자가 많은 경우 오리진 서버 부하를 줄여줍니다.

### 데이터 소스

- **실시간**: jjss.or.kr 에서 라이브 스크래핑 (urllib + regex)
- **예측**: Google Chart 임베디드 데이터 + 과거 히스토리 기반 캘리브레이션
- **기본값**: 알려진 시간대별 패턴으로 폴백

> **Vercel 환경에서 주의:** jjss.or.kr이 해외 IP를 차단하는 경우 실시간 스크래핑이 실패할 수 있습니다. 이 경우 내장된 기본 히스토리 패턴으로 자동 폴백되어 서비스가 계속 작동합니다.

### 헬스 체크

배포 후 다음 엔드포인트로 상태를 확인할 수 있습니다:

```bash
curl https://<your-project>.vercel.app/api/health
```

응답 예시:
```json
{
  "status": "healthy",
  "timestamp": "2026-06-02T09:32:45+09:00",
  "data_sources": {
    "live_cache": true,
    "chart_predictions_today": true,
    "historical_predictions": true
  },
  "version": "0.1.0"
}
```

### 문제 해결

| 문제 | 확인 사항 |
|------|----------|
| **500 에러** | `requirements.txt`에 `fastapi`가 명시되어 있는지 확인 |
| **jjss.or.kr 데이터 없음** | Vercel IP가 한국이 아닐 경우 차단될 수 있음 — 기본 패턴으로 폴백되므로 서비스는 정상 작동 |
| **빌드 실패** | `log` 탭에서 Python 버전 확인 — `.python-version`이 `3.12`인지 확인 |
| **배포 후 페이지 안 열림** | Vercel Dashboard → Project → Logs 에서 에러 확인 |

---

## 프로젝트 구조

```
pool-congestion/
├── main.py              # FastAPI 앱 (전체 로직, HTML 템플릿 포함)
├── test_main.py         # pytest 단위 테스트 (210개)
├── vercel.json          # Vercel 배포 설정
├── requirements.txt     # 프로덕션 의존성
├── pyproject.toml       # Python 프로젝트 설정 (로컬 개발)
├── uv.lock              # uv 잠금 파일 (로컬 개발)
├── .python-version      # Python 3.12
├── .vercelignore        # Vercel 배포 제외 목록
└── README.md            # 이 파일
```

## 라이선스

MIT
