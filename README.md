# V9 — AI SI 매뉴팩처링 플랫폼 v9

> **Claude 기반 SI 프로젝트 산출물 자동 생성 엔진**
>
> 보안강화 · 성능최적화 · 테스트강화 · 문서완성 | v8 → v9 주요 개선

## 개요

V9는 V8의 진화 버전으로, 다음 영역을 전면 개선했습니다:

- **보안**: 초기 관리자 비밀번호 환경변수화, SHA-256→bcrypt 비밀번호 해싱
- **버그 수정**: Null 포인터 3곳, 동기 파일 I/O, cascade silent failure, DB 트랜잭션 async
- **코드 품질**: 하드코딩 값 중앙화, executor 분리, 순환 의존성 제거
- **성능**: GZip 압축 미들웨어, CORS 명시적 설정, 요청 로깅
- **테스트**: 보안·null체크·미들웨어·모듈 4개 테스트 추가
- **문서**: README, CHANGELOG, 환경변수 가이드

## 빠른 시작

```bash
cd /Users/codename/Downloads/v9
pip install -r requirements.txt
python run.py
```

서버: `http://localhost:8000`  
건강도 체크: `curl http://localhost:8000/health`

## 환경변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `V9_ADMIN_PASSWORD` | 초기 관리자 비밀번호 | 랜덤 생성 |
| `V9_CORS_ORIGINS` | CORS 허용 origin | `http://localhost:3000` |
| `V9_API_CONCURRENCY` | 동시 API 호출 수 | 10 |

더 자세한 내용은 [`docs/ENVIRONMENT_VARS.md`](docs/ENVIRONMENT_VARS.md)를 참고하세요.

## 아키텍처

```
V9 엔진 (59,600줄)
├── api/server.py (4,342줄)
│   ├── FastAPI 라우터
│   ├── GZip + CORS 미들웨어 (v9 추가)
│   └── 요청 로깅 미들웨어 (v9 추가)
│
├── engine/
│   ├── core/ (상태머신, DAG, cascade)
│   ├── skills/executor.py (4,772줄)
│   │   ├── executor_gotcha.py (v9 분리)
│   │   ├── executor_heartbeat.py (v9 분리)
│   │   └── codegen/css_tokens.py (v9 분리)
│   ├── ai/ (모델 라우팅, 컨텍스트 조립)
│   ├── db/ (SQLite, 마이그레이션 026~030)
│   ├── lifecycle/ (startup, shutdown, watchdog)
│   └── config/thresholds.py (v9 확장)
│
└── tests/
    ├── test_v9_security.py (v9 신규)
    ├── test_v9_null_checks.py (v9 신규)
    ├── test_v9_middleware.py (v9 신규)
    └── test_v9_refactored_modules.py (v9 신규)
```

## V9 주요 개선사항

### 보안 (Phase 2)
- ❌ `admin_pw = "admin1234"` (하드코딩)
- ✅ `V9_ADMIN_PASSWORD` 환경변수 분리, 미설정 시 `secrets.token_urlsafe(24)` 랜덤 생성
- ❌ SHA-256 단순 해싱
- ✅ bcrypt 기반 비밀번호 (salt + KDF)

### 버그 수정 (Phase 3)
| 버그 | 수정 |
|------|------|
| `task_pair_node_id[:8]` → NullPointerError | `(task_pair_node_id or "")[:8]` |
| async 함수 내 `open()` 동기 호출 → 블로킹 | `await asyncio.to_thread(lambda: open(...).read())` |
| `except Exception: pass` → silent failure | `logger.warning()` 추가 |
| `begin_immediate()` 동기 호출 | `asyncio.to_thread` 래핑 |
| cascade `terminate_agent` 미주입 | Null 체크 + 경고 로깅 |

### 코드 품질 (Phase 4)
- 모든 모델 ID 문자열 → `ModelID` 상수 통일
- `max_tokens=8000` 등 7개 리터럴 → `OUTLINE_MAX_TOKENS` 등 상수화
- AccountRouter TTL 30초 → `ACCOUNT_CACHE_TTL` 상수화
- CLI 타임아웃 1200초 → 모델별 `CLI_TIMEOUT_HAIKU/SONNET/OPUS`

### 성능 최적화 (Phase 5)
- **GZip 압축**: 1KB 이상 응답 자동 압축
- **CORS**: 명시적 설정 (`V9_CORS_ORIGINS`)
- **요청 로깅**: HTTP 요청/응답 시간 기록

### 리팩토링 (Phase 6)
executor.py 4,772줄 중 안전한 로직만 분리:
- `executor_gotcha.py`: Gotcha 분류, DB 기록, 로드
- `codegen/css_tokens.py`: CSS 토큰 변환 (순수 함수)
- `executor_heartbeat.py`: 하트비트 감시 + 타임아웃 처리

### 테스트 (Phase 7)
4개 신규 테스트:
- `test_v9_security.py`: bcrypt, 환경변수, ModelID
- `test_v9_null_checks.py`: Null 안전성
- `test_v9_middleware.py`: thresholds 상수
- `test_v9_refactored_modules.py`: 분리 모듈

## 검증 방법

```bash
# 테스트 실행
pytest tests/test_v9_*.py -v

# 서버 건강도
curl http://localhost:8000/health

# GZip 확인 (1KB 이상)
curl -H "Accept-Encoding: gzip" http://localhost:8000/health -i | grep "Content-Encoding"

# CORS 확인
curl -H "Origin: http://localhost:3000" http://localhost:8000/ -i | grep "Access-Control"
```

## 버전 정보

- **V9**: 2026-04-17
- **V8**: 2024 (기존)
- Python: ≥3.11
- FastAPI: ≥0.111.0
- Uvicorn: ≥0.29.0

## 변경사항 상세

자세한 변경사항은 [`CHANGELOG.md`](CHANGELOG.md)를 참고하세요.

---

**Built with Claude** | AI SI Manufacturing Platform v9
