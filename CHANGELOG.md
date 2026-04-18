# Changelog

모든 주요 변경사항이 이 파일에 기록됩니다.

## [9.0.0] - 2026-04-17

### 주요 개선 (Major Improvements)

이 버전은 V8에서 대규모 보안강화, 버그 수정, 코드품질 개선을 수행했습니다.

### Security

- **초기 관리자 비밀번호**: 하드코딩된 `"admin1234"` 제거
  - 환경변수 `V9_ADMIN_PASSWORD`로 분리
  - 미설정 시 `secrets.token_urlsafe(24)`로 랜덤 생성
  - 서버 로그에 임시 비밀번호 출력

- **비밀번호 해싱**: SHA-256 → **bcrypt** 교체
  - `bcrypt.hashpw(password, bcrypt.gensalt())` 사용
  - salt + KDF 기반 해싱 (dictionary attack 방어)
  - 의존성 추가: `bcrypt>=4.0.1`

- **모델 ID 불일치 버그**: `"claude-sonnet-4-20250514"` → `ModelID.SONNET` 통일
  - 잘못된 모델 버전명 사용 방지

### Fixed

- **executor.py:3154, 3288, 3294** — `task_pair_node_id` Null 체크 누락
  - Before: `node.task_pair_node_id[:8]` → `TypeError` 발생
  - After: `(node.task_pair_node_id or "")[:8]` → 안전한 문자열 반환

- **executor.py:3519** — async 함수 내 동기 파일 I/O
  - Before: `with open(...) as f: f.read()` → 이벤트 루프 블로킹
  - After: `await asyncio.to_thread(lambda: open(...).read())`

- **executor_cascade.py** — silent failure (except: pass)
  - Before: 예외 무시 → 에러 추적 불가
  - After: `logger.warning("cascade re-enqueue failed: %s", e)`

- **engine/db/adapter.py:197-205** — `begin_immediate()` 동기 호출
  - Before: `BEGIN`, `COMMIT`, `ROLLBACK` 직접 호출 → 블로킹
  - After: `await asyncio.to_thread()` 래핑

- **engine/core/cascade.py:122-132** — `terminate_agent` 미주입 시 무음 실패
  - Before: 에이전트 종료 안 됨 → 리소스 누수
  - After: `if self._terminate_agent:` 체크 + 경고 로깅

### Changed

- **모델 ID 중앙화**: 모든 모델 문자열을 `ModelID` 클래스 상수로 통일
  - `executor.py:674, 1076` 등 7+ 곳의 `"claude-sonnet-4-6"` → `ModelID.SONNET`
  - 모델 버전 변경 시 `ModelID` 클래스만 수정

- **숫자 리터럴 중앙화**: `thresholds.py`에 추가 상수화
  - `OUTLINE_MAX_TOKENS = 8000`
  - `JSON_SPLIT_THRESHOLD = 50000`
  - `QA_CONTEXT_TRUNCATE = 3500`
  - `QA_PASS_THRESHOLD = 50`
  - `QA_PARTIAL_THRESHOLD = 30`

- **백오프 중복 제거**: `model_adapter.py`의 `_OVERLOAD_BACKOFFS`
  - Before: `(30, 60, 120)` 하드코딩
  - After: `TRANSIENT_BACKOFFS` import

- **캐시 TTL 상수화**:
  - `ACCOUNT_CACHE_TTL = 30.0` (초)

- **CLI 타임아웃 모델별 설정**:
  - `CLI_TIMEOUT_HAIKU = 300` (5분)
  - `CLI_TIMEOUT_SONNET = 600` (10분)
  - `CLI_TIMEOUT_OPUS = 1200` (20분)

### Performance

- **GZip 미들웨어**: 1KB 이상 응답 자동 압축
  ```python
  app.add_middleware(GZipMiddleware, minimum_size=1024)
  ```

- **CORS 미들웨어**: 명시적 설정
  ```python
  allow_origins = os.environ.get("V9_CORS_ORIGINS", "http://localhost:3000").split(",")
  ```

- **요청 로깅 미들웨어**: HTTP 요청/응답 시간 기록
  ```python
  logger.info("%s %s → %d (%.3fs)", method, path, status, elapsed)
  ```

### Refactored

- **executor.py 분리** (4,772줄 → 4,500줄 + 3개 모듈)
  - `engine/skills/executor_gotcha.py` (신규, 77줄)
    - `classify_gotcha()`, `record_gotcha()`, `load_gotchas_for_prompt()`
  - `engine/skills/codegen/css_tokens.py` (신규, 47줄)
    - `tokens_to_css_vars()`, `build_style_from_design_tokens()`
  - `engine/skills/executor_heartbeat.py` (신규, 76줄)
    - `executor_with_heartbeat()` 팩토리, 타임아웃 처리 개선

### Added (Tests)

- `tests/test_v9_security.py`: 보안 강화 (bcrypt, 환경변수, ModelID)
- `tests/test_v9_null_checks.py`: Null 안전성
- `tests/test_v9_middleware.py`: 미들웨어, thresholds 상수
- `tests/test_v9_refactored_modules.py`: 분리 모듈

### Added (Docs)

- `README.md`: 프로젝트 소개, 빠른 시작, 아키텍처
- `CHANGELOG.md`: 변경사항 (이 파일)
- `docs/ENVIRONMENT_VARS.md`: 환경변수 가이드

### Deprecations

없음

### Known Issues

없음 (V8의 알려진 이슈 없음)

### Migration Guide (V8 → V9)

1. **bcrypt 의존성 추가**:
   ```bash
   pip install bcrypt>=4.0.1
   ```

2. **환경변수 설정** (선택사항):
   ```bash
   export V9_ADMIN_PASSWORD="your-secure-password"
   export V9_CORS_ORIGINS="http://localhost:3000,https://example.com"
   ```

3. **기존 비밀번호 마이그레이션**:
   - V8의 SHA-256 해시는 호환되지 않음
   - 모든 사용자 비밀번호를 재설정해야 함
   - `api/server.py`의 초기 관리자 계정도 재생성됨

4. **모듈 import 변경**:
   - `from engine.skills.executor_gotcha import classify_gotcha` (새로 추가됨)
   - `from engine.skills.codegen.css_tokens import build_style_from_design_tokens`
   - `from engine.skills.executor_heartbeat import executor_with_heartbeat`

---

[9.0.0]: https://github.com/codename-2501/v9/releases/tag/9.0.0
