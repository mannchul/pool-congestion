# 🏊 라온체육센터 수영장 혼잡도

전주 라온체육센터 수영장의 시간대별 예상 혼잡도를 확인하는 웹 애플리케이션입니다.

## 기술 스택

- **Backend:** Python 3.11, FastAPI
- **Frontend:** Vanilla JavaScript, Canvas API (CSS 내장)
- **배포:** Vercel (Serverless Functions)

## 로컬 개발

```bash
# 의존성 설치
uv sync

# 또는 pip 사용
pip install -r requirements.txt

# 개발 서버 실행
python main.py

# http://localhost:8000 에서 확인
```

### 테스트 실행

```bash
python -m pytest test_main.py -v
```

## Vercel 배포

### 1. Vercel CLI로 배포

```bash
# Vercel CLI 설치
npm i -g vercel

# 로그인 (처음 한 번)
vercel login

# 프로젝트 루트(pool-congestion/)에서 배포
cd pool-congestion
vercel

# 프로덕션 배포
vercel --prod
```

### 2. GitHub 연동 (Vercel Dashboard)

1. [Vercel Dashboard](https://vercel.com) 에서 **Add New → Project**
2. GitHub 저장소를 연결하고 `pool-congestion/` 디렉토리를 선택
3. Framework Preset은 자동 감지되며, 추가 설정 없이 **Deploy**
4. 배포 완료 후 `https://<project>.vercel.app` 에서 확인

### 배포 구조

```
pool-congestion/
├── main.py            # FastAPI 앱 (Vercel이 app 변수를 ASGI로 인식)
├── vercel.json        # Vercel 설정 (Python 빌더, 라우팅)
├── requirements.txt   # 의존성 (Vercel에서 자동 설치)
└── .python-version    # Python 3.11
```

### 환경 변수 (필요시)

Vercel Dashboard → Project Settings → Environment Variables 에서 추가:

| 변수명 | 설명 |
|--------|------|
| `PYTHON_VERSION` | `3.11` (`.python-version`과 일치) |

## API 엔드포인트

| 경로 | 설명 |
|------|------|
| `GET /` | 메인 대시보드 페이지 |
| `GET /api/congestion` | 현재 혼잡도 + 시간대별 예보 |
| `GET /api/daily-trend` | 오늘 하루 혼잡도 트렌드 |

## 라이선스

MIT
