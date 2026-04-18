# 환경변수 가이드 (V9)

V9 엔진이 지원하는 모든 환경변수를 정리했습니다.

## 보안 (Security)

### `V9_ADMIN_PASSWORD`

**설명**: 초기 관리자 계정의 비밀번호

**타입**: 문자열 (string)

**기본값**: 미설정 시 `secrets.token_urlsafe(24)` 자동 생성

**예시**:
```bash
export V9_ADMIN_PASSWORD="YourSecurePassword123!@#"
```

**주의사항**:
- 프로덕션 환경에서는 반드시 설정 권장
- 미설정 시 서버 로그에 임시 비밀번호 출력
- SHA-256이 아닌 bcrypt 해싱 사용

---

## 네트워킹 (Networking)

### `V9_CORS_ORIGINS`

**설명**: CORS 요청을 허용할 origin 목록 (쉼표로 구분)

**타입**: 문자열 (CSV)

**기본값**: `http://localhost:3000`

**예시**:
```bash
# 로컬 개발
export V9_CORS_ORIGINS="http://localhost:3000"

# 프로덕션 (여러 origin)
export V9_CORS_ORIGINS="https://example.com,https://app.example.com"
```

**주의사항**:
- 와일드카드 `*` 사용은 보안 위험 (기본값으로 제한)
- 프로덕션에서는 명시적 origin만 허용

### `V9_PORT`

**설명**: HTTP 서버 listen 포트

**타입**: 정수 (int)

**기본값**: `8000`

**예시**:
```bash
export V9_PORT=8080
```

---

## API & LLM

### `V9_API_CONCURRENCY`

**설명**: 동시 API 호출 수 (Claude CLI 요청)

**타입**: 정수 (int)

**기본값**: `10`

**예시**:
```bash
export V9_API_CONCURRENCY=20
```

### `V9_CLI_AUTH_TYPE`

**설명**: Claude CLI 인증 방식

**타입**: 문자열

**가능한 값**:
- `oauth` (기본): OAuth 토큰 사용 (Max/Pro plan)
- `api_key` (비활성): API 키 직접 사용 (권장하지 않음, V9는 oauth만 지원)

**예시**:
```bash
export V9_CLI_AUTH_TYPE="oauth"
```

### `V9_CLI_TIMEOUT`

**설명**: Claude CLI 호출 타임아웃 (초)

**타입**: 정수 (int)

**기본값**: `1200` (20분)

**주의사항**:
- V9에서는 모델별 타임아웃 설정 권장
- `OUTLINE_MAX_TOKENS`, `CLI_TIMEOUT_HAIKU` 등 `thresholds.py` 상수 참고

---

## 데이터베이스

### `V9_DB_PATH`

**설명**: SQLite 데이터베이스 파일 경로

**타입**: 문자열

**기본값**: `./engine.db` (현재 디렉토리)

**예시**:
```bash
export V9_DB_PATH="/var/lib/v9/engine.db"
```

### `V9_DB_BACKUP_DIR`

**설명**: 자동 백업 저장 디렉토리

**타입**: 문자열

**기본값**: `./backups`

**예시**:
```bash
export V9_DB_BACKUP_DIR="/var/backups/v9"
```

---

## 로깅 & 모니터링

### `V9_LOG_LEVEL`

**설명**: 로그 레벨

**타입**: 문자열

**가능한 값**: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

**기본값**: `INFO`

**예시**:
```bash
export V9_LOG_LEVEL="DEBUG"  # 개발 환경
export V9_LOG_LEVEL="WARNING"  # 프로덕션
```

### `V9_LOG_FORMAT`

**설명**: 로그 포맷

**타입**: 문자열

**가능한 값**:
- `json` (기본): JSON 형식
- `text`: 텍스트 형식

**예시**:
```bash
export V9_LOG_FORMAT="json"
```

### `PROMETHEUS_PORT`

**설명**: Prometheus 메트릭 서버 포트

**타입**: 정수

**기본값**: `9090`

**예시**:
```bash
export PROMETHEUS_PORT=9090
curl http://localhost:9090/metrics
```

---

## 캐싱

### `V9_CACHE_TTL`

**설명**: 일반 캐시 TTL (초)

**타입**: 정수

**기본값**: `3600` (1시간)

### `V9_ACCOUNT_CACHE_TTL`

**설명**: 활성 계정 목록 캐시 TTL (초)

**타입**: 정수

**기본값**: `30`

**참고**: `thresholds.py`의 `ACCOUNT_CACHE_TTL` 상수

---

## 버전 & 정보

### `V9_VERSION`

**설명**: 엔진 버전 (읽기 전용)

**타입**: 문자열

**값**: `9.0.0`

---

## 개발 환경 예시

```bash
# .env (또는 shell profile에 추가)

# 보안
export V9_ADMIN_PASSWORD="dev-password-123"

# 네트워킹
export V9_PORT=8000
export V9_CORS_ORIGINS="http://localhost:3000"

# API
export V9_API_CONCURRENCY=10
export V9_CLI_TIMEOUT=1200

# 로깅
export V9_LOG_LEVEL="DEBUG"
export V9_LOG_FORMAT="text"

# 데이터베이스
export V9_DB_PATH="./engine.db"
```

서버 시작:
```bash
source .env
python run.py
```

---

## 프로덕션 환경 예시

```bash
# systemd service file에서 또는 docker-compose.yml

V9_ADMIN_PASSWORD=<strong-password>
V9_PORT=8000
V9_CORS_ORIGINS=https://example.com,https://app.example.com
V9_API_CONCURRENCY=50
V9_LOG_LEVEL=WARNING
V9_LOG_FORMAT=json
V9_DB_PATH=/var/lib/v9/engine.db
V9_DB_BACKUP_DIR=/var/backups/v9
PROMETHEUS_PORT=9090
```

---

## 환경변수 로드 순서

1. 시스템 환경변수
2. `.env` 파일 (있으면)
3. `.env.local` 파일 (있으면, 버전 관리 제외)
4. 하드코딩된 기본값 (fallback)

---

더 자세한 내용은 [`README.md`](../README.md)를 참고하세요.
