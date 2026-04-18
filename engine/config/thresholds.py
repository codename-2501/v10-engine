"""엔진 전역 임계값·상수 중앙화 (S1-3, v9 확장).

이전엔 `executor.py`, `harness.py`, `watchdog.py` 등 여러 파일에 magic number
가 산재해 있어 변경 시 추적·일관성 유지 어려움. 모든 상수를 이 파일로 모아
import 하는 형태로 점진 리팩토링.

새로 상수 추가 시 이 파일에 정의 후 각 모듈에서 `from engine.config.thresholds
import X` 패턴으로 사용.

v9에서는 CLI 타임아웃, 캐시 TTL, QA 점수 임계값 등을 추가로 중앙화.
"""

from __future__ import annotations

# ────────────────────────────────────────────────────────────────────────
# 버전
# ────────────────────────────────────────────────────────────────────────

ENGINE_VERSION = "9.0.0"


# ────────────────────────────────────────────────────────────────────────
# LLM 호출 / 토큰
# ────────────────────────────────────────────────────────────────────────

# 노드 누적 토큰 상한 — 초과 시 SUSPENDED (무한 루프 방지).
# S10: 300K → 600K 상향. 대형 chunk_items 분할 산출물 (62개 아이템 × 평균
# 5.7K = 358K) 이 정상적으로 포함되도록. 진짜 폭주 (수 M 토큰) 는 여전히 차단.
NODE_TOKEN_LIMIT: int = 600_000

# S10 (참고용, 현재 미사용): chunk_items 사용 시 아이템당 추가 토큰 예산.
# 현재는 NODE_TOKEN_LIMIT flat 상향으로 대응.
CHUNK_ITEM_TOKEN_BUDGET: int = 8_000

# 기본 max_tokens (context_assembler의 TOKEN_BUDGET과 매핑)
DEFAULT_MAX_OUTPUT: int = 16_000

# JSON 산출물 자동 오버라이드
JSON_MAX_OUTPUT_OVERRIDE: int = 32_000

# Truncation retry·재시도 시 확장 최대 cap
MAX_OUTPUT_CAP: int = 64_000

# Outline-first (F4) 호출 시 max_tokens (리스트 전용이라 작음)
OUTLINE_MAX_TOKENS: int = 8_000

# Document 자가 재시도 min floor (retry >= 1 시)
RETRY_MIN_OUTPUT_FLOOR: int = 24_000

# Retry-aware max_tokens multiplier (prev_size × multiplier)
RETRY_MULT_NONE: float = 1.3    # retry_count == 0
RETRY_MULT_FIRST: float = 1.6   # retry_count == 1
RETRY_MULT_DEEP: float = 2.0    # retry_count >= 2

# Truncation 감지 시 자동 확장 multiplier
TRUNCATION_EXPAND_MULT: float = 1.5


# ────────────────────────────────────────────────────────────────────────
# QA 점수 임계값
# ────────────────────────────────────────────────────────────────────────

# AI QA PASS 임계 (점수 >= 이 값이면 FAIL verdict여도 PASS 승격)
QA_SCORE_PASS: int = 50

# QA partial patch 임계 (점수 >= 이 값이면 전체 재생성 대신 부분 패치)
QA_SCORE_PARTIAL: int = 30


# ────────────────────────────────────────────────────────────────────────
# F4 섹션 분할
# ────────────────────────────────────────────────────────────────────────

# Outline-first 발동 최소 섹션 수 (이 이상일 때만 외곽 호출)
OUTLINE_MIN_SECTIONS: int = 3

# Outline 응답에서 파싱된 항목이 이 개수 미만이면 sanity fail
OUTLINE_MIN_PARSED_ITEMS: int = 3

# 섹션 내 min_items 체크 기본값 (spec 미지정 시 사용)
DEFAULT_SECTION_MIN_ITEMS: int = 0

# 섹션 target_tokens 범위 clamp
SECTION_TARGET_MIN: int = 3_000
SECTION_TARGET_MAX: int = 16_000


# ────────────────────────────────────────────────────────────────────────
# 재시도·Stall 방어
# ────────────────────────────────────────────────────────────────────────

# 노드 기본 max_retries
DEFAULT_MAX_RETRIES: int = 3

# Stall 감지 임계 (retry_count 가 아닌 stall_count — QA 연속 FAIL 상한)
STALL_LIMIT: int = 2

# Watchdog 주기 (초)
WATCHDOG_INTERVAL_SECONDS: int = 300

# IN_PROGRESS 좀비 감지 임계 (분)
TASK_STUCK_THRESHOLD_MINUTES: int = 30
QA_STUCK_THRESHOLD_MINUTES: int = 10

# SUSPENDED → NOT_STARTED 재개 전 최소 쿨다운 (분)
SUSPENDED_MIN_COOLDOWN_MINUTES: int = 3
SUSPENDED_MAX_RESUMES: int = 3

# 일시 오류 왕복 무한 방지용 failure_reasons 누적 bytes 상한
RESUME_RUNAWAY_FR_BYTES: int = 6_000


# ────────────────────────────────────────────────────────────────────────
# 네트워크 재시도
# ────────────────────────────────────────────────────────────────────────

# API 과부하·일시 오류 재시도 백오프 (초)
TRANSIENT_BACKOFFS: tuple[int, ...] = (30, 60, 120)

# 최대 재시도 횟수
TRANSIENT_MAX_RETRIES: int = 3


# ────────────────────────────────────────────────────────────────────────
# Harness 금지어 자동수정 (harness_auto_fix)
# ────────────────────────────────────────────────────────────────────────

# 자동수정 치환 상한 (이 이상은 LLM 남용으로 판단 → no-op)
MAX_AUTO_FIX_FORBIDDEN: int = 5


# ────────────────────────────────────────────────────────────────────────
# 화면/SCR 커버리지 (DESIGN)
# ────────────────────────────────────────────────────────────────────────

# 화면 커버리지 초기 임계 (executor에서 retry별 +0.1 증분, 상한 1.0)
SCREEN_COVERAGE_BASE: float = 0.8

# 증분
SCREEN_COVERAGE_RETRY_STEP: float = 0.1

# 상한 (1.0 = 100%)
SCREEN_COVERAGE_CAP: float = 1.0


# ────────────────────────────────────────────────────────────────────────
# DEFINE 교차참조 검증
# ────────────────────────────────────────────────────────────────────────

# 화면목록 SCR 중 유저플로우 미등장 허용 비율 상한
DEFINE_DANGLING_SCREENS_MAX_RATIO: float = 0.4

# 백로그 ↔ 요구사항 키워드 매치 최소 비율
DEFINE_BACKLOG_REQ_MATCH_MIN: float = 0.5


# ────────────────────────────────────────────────────────────────────────
# 엣지 케이스 정책 (S3-7)
# ────────────────────────────────────────────────────────────────────────

# 대규모 프로젝트 — 화면 100+ 이상이면 sub-chunk 분할 적용
LARGE_PROJECT_SCREEN_THRESHOLD: int = 100

# F4 sub-chunking: 한 섹션 내 항목 수가 이 값을 넘으면 chunk 단위로 분할 호출
SUBCHUNK_ITEMS_PER_BATCH: int = 25

# 토큰 폭주 — engagement 단위 PAUSE 임계 (engagement_budget.py 와 동기화)
ENGAGEMENT_TOKEN_PAUSE_RATIO: float = 1.0
ENGAGEMENT_TOKEN_WARN_RATIO: float = 0.8

# 네트워크 장기 단절 — transient retry 상한 확장
TRANSIENT_LONG_OUTAGE_MAX_RETRIES: int = 6  # 기본 3보다 큰 long-outage mode

# Long-outage 알림 임계 (분 단위 — 이후 operator 알림)
LONG_OUTAGE_ALERT_MINUTES: int = 30

# 동시 편집 — 낙관적 잠금 충돌 시 자동 merge 시도 횟수
OPTIMISTIC_LOCK_MERGE_RETRIES: int = 2

# ARCHIVED 라이프사이클 — 마지막 활동 후 N일 이후 cold storage 후보
ARCHIVED_COLD_STORAGE_DAYS: int = 90


# ────────────────────────────────────────────────────────────────────────
# i18n / Locale (S3-6)
# ────────────────────────────────────────────────────────────────────────

# 기본 locale (engagement.locale 미지정 시)
DEFAULT_LOCALE: str = "ko"

# 지원 locale 목록 (spec prompt 변수 분기용)
SUPPORTED_LOCALES: tuple[str, ...] = ("ko", "en", "ko-en")

# locale 별 placeholder 대체어 (auto_fix 에서 참조)
LOCALE_PLACEHOLDER_REPLACEMENTS: dict[str, dict[str, str]] = {
    "ko": {
        "TBD": "미지정",
        "TODO": "다음 단계 진행 예정",
        "FIXME": "수정 필요",
    },
    "en": {
        "TBD": "to be determined",
        "TODO": "to be addressed",
        "FIXME": "to be fixed",
    },
    "ko-en": {
        "TBD": "미지정 (TBD)",
        "TODO": "예정 (TODO)",
        "FIXME": "수정필요 (FIXME)",
    },
}


# ────────────────────────────────────────────────────────────────────────
# v9 추가: 캐시, 타임아웃, QA 점수 임계값
# ────────────────────────────────────────────────────────────────────────

# 계정 캐시 TTL (활성 계정명 조회)
ACCOUNT_CACHE_TTL: float = 30.0

# CLI 호출 모델별 타임아웃 (초)
CLI_TIMEOUT_HAIKU: int = 300      # 5분
CLI_TIMEOUT_SONNET: int = 600     # 10분
CLI_TIMEOUT_OPUS: int = 1200      # 20분

# QA 점수 임계값
QA_PASS_THRESHOLD: int = 50       # 통과 점수
QA_PARTIAL_THRESHOLD: int = 30    # 부분 통과 점수

# executor.py 하드코딩 값 중앙화
OUTLINE_MAX_TOKENS: int = 8000
JSON_SPLIT_THRESHOLD: int = 50000
QA_CONTEXT_TRUNCATE: int = 3500
