"""
engine/intake/processor.py  ─ v9
IntakeProcessor — AI SI 인테이크 폼 → Engagement + 프로젝트/DAG/노드 자동 생성.

변경 이력 (v8 → v9):
  ① 필드명 정규화: camelCase(구 web-dev 폼) + snake_case(신규 AI SI 폼, 공개/내부) 동시 지원
     - _resolve()  : 단일 값 — 우선순위 순으로 키 탐색
     - _resolve_list(): 리스트 값 — array 및 쉼표 구분 string 모두 처리
  ② AI SI 도메인 NODE_TEMPLATES 전면 교체 (web-dev 템플릿 제거)
  ③ AI_SCOPE_TO_PHASE: AI SI 범위(scope) → Phase 매핑 테이블 신규 추가
  ④ _determine_components: AI SI 컴포넌트 구조 (MASTER / SI / MLOPS / DATA)
  ⑤ _match_templates: "scope" 필드 정확히 읽기, always 트리거 보존
  ⑥ INTRA_PHASE_DEPS: AI SI 워크플로우 순서 의존성으로 교체
  ⑦ _insert_phase_gates: 실제 노드가 있는 Phase 사이에만 Gate 삽입
  ⑧ _create_project: component_type → AI SI 값, project_type 다중 스키마 지원

설계 불변 조건 (v8 그대로 유지):
  - Rule 9: TASK 노드마다 QA 노드 자동 쌍 생성
  - Phase 간 Gate 노드 자동 삽입 (단, 노드가 있는 Phase 사이에만)
  - IntakeResult 인터페이스 불변
  - PHASE_ORDER: state_machine.PHASE_ORDER 사용 (코어 변경 없음)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from engine.db.adapter import DatabaseAdapter
from engine.core.env_config_generator import generate_env_defaults

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 필드 정규화 헬퍼 (v9 핵심 추가)
# ---------------------------------------------------------------------------

def _resolve(raw: dict, *keys: str, default: Any = "") -> Any:
    """
    여러 키를 우선순위 순으로 탐색해 첫 번째 존재하는 값을 반환.
    None / 빈 문자열은 건너뜀.
    공개 폼(snake_case) + 내부 폼(snake_case) + 구 폼(camelCase) 모두 지원.
    """
    for k in keys:
        v = raw.get(k)
        if v is not None and v != "":
            return v
    return default


# 공개 폼 구버전에서 한글 표시명으로 저장된 scope 값을 영문 키로 변환.
# 신규 폼은 처음부터 영문 키를 전송하므로 이 맵은 backward-compat 전용.
_KOREAN_SCOPE_MAP: dict[str, str] = {
    "AI 모델 개발":       "ai_model",
    "챗봇·자동화":        "chatbot",
    "LLM 적용":           "llm",
    "비전·영상 분석":     "vision",
    "예측·이상감지":      "prediction",
    "데이터 파이프라인":  "data_pipeline",
    "MLOps 구축":         "mlops",
    "시스템 통합(SI)":    "si",
    "컨설팅·PoC":         "consulting",
}


def _resolve_list(raw: dict, *keys: str) -> list[str]:
    """
    여러 키를 우선순위 순으로 탐색.
    - list 타입 → 그대로 반환
    - str 타입  → 쉼표로 분리 후 strip
    - dict 타입 → True인 키만 추출 (project_types 객체 형식)
    없으면 []
    공개 폼 구버전(한글 표시명)이 저장된 경우 영문 키로 변환.
    """
    for k in keys:
        v = raw.get(k)
        if isinstance(v, list) and v:
            items = [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            items = [x.strip() for x in v.split(",") if x.strip()]
        elif isinstance(v, dict):
            items = [k2 for k2, v2 in v.items() if v2]
        else:
            continue
        return [_KOREAN_SCOPE_MAP.get(i, i) for i in items]
    return []


# ---------------------------------------------------------------------------
# AI SI 도메인 상수
# ---------------------------------------------------------------------------

# AI SI 범위(scope) → Phase 매핑 (state_machine.PHASE_ORDER 기준)
# PHASE_ORDER: DEFINE → DESIGN → BUILD → VERIFY → DELIVER
AI_SCOPE_TO_PHASE: dict[str, str] = {
    "si":           "DESIGN",     # 시스템 통합 → 설계 단계
    "consulting":   "DEFINE",     # 컨설팅·PoC → 정의 단계
    "ai_model":     "BUILD",      # AI 모델 개발 → 구현 단계
    "vision":       "BUILD",      # 비전·영상 분석 → 구현 단계
    "prediction":   "BUILD",      # 예측·이상감지 → 구현 단계
    "llm":          "BUILD",      # LLM 적용 → 구현 단계
    "chatbot":      "BUILD",      # 챗봇·자동화 → 구현 단계
    "data_pipeline":"BUILD",      # 데이터 파이프라인 → 구현 단계
    "mlops":        "BUILD",      # MLOps → 구현 단계
}

MODEL_MAP = {
    "opus":   "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}


# ---------------------------------------------------------------------------
# AI SI 노드 템플릿 레지스트리 (v9 전면 교체)
# ---------------------------------------------------------------------------
# trigger 규칙:
#   always: True     → 모든 프로젝트에 항상 생성
#   scope: [...]     → _resolve_list(raw, "scope") 중 하나라도 포함 시 생성

# ---------------------------------------------------------------------------
# 전체 산출물 목록 — 신규/고도화 모두 포함, 조건부 실행
# ---------------------------------------------------------------------------
# when 조건:
#   "always"         — 프로젝트 유형 무관, 항상 실행
#   "upgrade"        — 고도화/리뉴얼 전용 (신규면 SKIPPED)
#   "new"            — 신규 구축 전용 (고도화면 SKIPPED)
#   ["scope1", ...]  — 해당 scope 포함 시 실행 (미포함이면 SKIPPED)

NODE_TEMPLATES: list[dict] = [

    # ══════════════════════════════════════════════════════════════════════
    # DEFINE (정의) — "뭘 만들지"
    # ══════════════════════════════════════════════════════════════════════
    {"phase": "DEFINE", "nodes": [
        # 관공서 전용
        {"name": "프로젝트 착수 보고서",                      "model": "sonnet", "when": ["public"]},
        {"name": "프로젝트 관리 계획서 (PMP)",                "model": "sonnet", "when": ["public"]},
        # 고도화 전용
        {"name": "AS-IS 현행 시스템 분석서",                   "model": "opus",   "when": "upgrade"},
        {"name": "TO-BE 목표 시스템 정의서",                   "model": "opus",   "when": "upgrade"},
        {"name": "GAP 분석서",                                 "model": "opus",   "when": "upgrade"},
        # 신규 전용
        {"name": "PRD (제품 요구사항 정의서)",                 "model": "opus",   "when": "new"},
        # 공통
        {"name": "기능 백로그 (Product Backlog)",              "model": "opus",   "when": "always"},
        {"name": "사용자 흐름도 (User Flow)",                  "model": "sonnet", "when": "always"},
        {"name": "서비스 운영 정책서 (SLA·과금·보안)",         "model": "opus",   "when": "always"},
        # 컨설팅·PoC
        {"name": "현황 진단 리포트",                           "model": "opus",   "when": ["consulting"]},
        {"name": "PoC 설계서",                                 "model": "opus",   "when": ["consulting"]},
        {"name": "기대효과 분석 (ROI)",                        "model": "sonnet", "when": ["consulting"]},
        {"name": "리스크 관리 계획서",                         "model": "sonnet", "when": "always"},
    ]},

    # ══════════════════════════════════════════════════════════════════════
    # DESIGN (설계) — "어떻게 만들지"
    # ══════════════════════════════════════════════════════════════════════
    {"phase": "DESIGN", "nodes": [
        # 고객 납품 산출물
        {"name": "시스템 아키텍처 설계서 (HLD)",               "model": "opus",   "when": "always"},
        {"name": "IA (정보 구조도)",                           "model": "sonnet", "when": "always"},
        {"name": "화면 목록 정의서",                           "model": "sonnet", "when": "always"},
        {"name": "화면 설계서 (와이어프레임+스토리보드)",       "model": "opus",   "when": "always"},
        # 내부 산출물
        {"name": "API 설계서",                                 "model": "opus",   "when": "always"},
        {"name": "DB 설계서 (ERD·테이블 정의)",                "model": "opus",   "when": "always"},
        {"name": "보안 설계서 (ISMS)",                         "model": "opus",   "when": "always"},
        {"name": "컴포넌트 정의서 (디자인 시스템)",             "model": "sonnet", "when": "always"},
        {"name": "상태 정의서 (State Definition)",             "model": "sonnet", "when": "always"},
        # v10 컴포넌트 조합 모델: UI 디자인 시안을 토큰+라이브러리+레시피+조립으로 대체
        {"name": "디자인 토큰",                                "model": "sonnet", "when": "always"},
        {"name": "컴포넌트 라이브러리",                         "model": "sonnet", "when": "always"},
        {"name": "컴포넌트 레지스트리",                         "model": "sonnet", "when": "always"},
        {"name": "페이지 레시피",                               "model": "sonnet", "when": "always"},
        {"name": "페이지 조립",                                 "model": "sonnet", "when": "always"},
        {"name": "UI 디자인 시안",                             "model": "opus",   "when": "always"},
        {"name": "인터페이스 명세서 (ICD)",                    "model": "sonnet", "when": "always"},
        # AI/ML 전용
        {"name": "모델 아키텍처 설계서",                       "model": "opus",   "when": ["ai_model", "vision", "prediction"]},
        # SI 전용
        {"name": "시스템 통합 아키텍처 설계",                  "model": "opus",   "when": ["si"]},
        {"name": "데이터 매핑 테이블",                         "model": "sonnet", "when": ["si"]},
    ]},

    # ══════════════════════════════════════════════════════════════════════
    # BUILD (구현) — "만들기"
    # ══════════════════════════════════════════════════════════════════════
    {"phase": "BUILD", "nodes": [
        # 공통 코드 생성
        {"name": "개발표준 정의서 (코드 컨벤션·브랜치 전략)",  "model": "sonnet", "when": "always"},
        {"name": "프론트엔드 공통 인프라",                     "model": "sonnet", "when": "always"},
        {"name": "프론트엔드 컴포넌트 구현",                   "model": "sonnet", "when": "always"},
        {"name": "백엔드 API 구현",                           "model": "opus",   "when": "always"},
        {"name": "DB 스키마 및 마이그레이션 구현",              "model": "sonnet", "when": "always"},
        # AI 모델
        {"name": "탐색적 데이터 분석 (EDA) 리포트",            "model": "opus",   "when": ["ai_model"]},
        {"name": "모델 선정 보고서",                           "model": "opus",   "when": ["ai_model"]},
        {"name": "데이터 전처리 파이프라인",                   "model": "sonnet", "when": ["ai_model"]},
        {"name": "모델 학습 스크립트",                         "model": "sonnet", "when": ["ai_model"]},
        {"name": "모델 성능 평가 리포트",                      "model": "sonnet", "when": ["ai_model"]},
        # 비전
        {"name": "비전 모델 아키텍처 설계",                    "model": "opus",   "when": ["vision"]},
        {"name": "학습·추론 파이프라인 구현",                  "model": "sonnet", "when": ["vision"]},
        # 예측
        {"name": "예측 모델 개발",                             "model": "sonnet", "when": ["prediction"]},
        {"name": "이상감지 임계값 최적화",                     "model": "sonnet", "when": ["prediction"]},
        # LLM
        {"name": "LLM 프롬프트 엔지니어링 설계서",            "model": "opus",   "when": ["llm"]},
        {"name": "RAG 파이프라인 구축",                        "model": "sonnet", "when": ["llm"]},
        {"name": "LLM 서비스 API 개발",                        "model": "sonnet", "when": ["llm"]},
        # 챗봇
        {"name": "챗봇 시나리오 설계서",                       "model": "opus",   "when": ["chatbot"]},
        {"name": "대화 흐름 구현",                             "model": "sonnet", "when": ["chatbot"]},
        # 데이터 파이프라인
        {"name": "데이터 수집 파이프라인 설계",                "model": "opus",   "when": ["data_pipeline"]},
        {"name": "데이터 정제·변환 (ETL/ELT) 구현",           "model": "sonnet", "when": ["data_pipeline"]},
        # MLOps
        {"name": "MLOps 환경 구성",                            "model": "opus",   "when": ["mlops"]},
        {"name": "모델 서빙 API 배포",                         "model": "sonnet", "when": ["mlops"]},
    ]},

    # ══════════════════════════════════════════════════════════════════════
    # VERIFY (검증) — "제대로 만들었는지"
    # ══════════════════════════════════════════════════════════════════════
    {"phase": "VERIFY", "nodes": [
        # 고객 납품
        {"name": "테스트 계획서",                              "model": "sonnet", "when": "always"},
        {"name": "테스트 시나리오",                            "model": "sonnet", "when": "always"},
        {"name": "UAT 시나리오 및 결과",                       "model": "opus",   "when": "always"},
        {"name": "테스트 결과 보고서",                         "model": "sonnet", "when": "always"},
        {"name": "결함 리스트",                                "model": "sonnet", "when": "always"},
        # 내부
        {"name": "단위 테스트 결과",                           "model": "sonnet", "when": "always"},
        {"name": "통합 테스트 결과",                           "model": "sonnet", "when": "always"},
        {"name": "성능 테스트 결과",                           "model": "sonnet", "when": "always"},
        {"name": "보안 점검 결과",                             "model": "sonnet", "when": "always"},
        # AI/ML 전용
        {"name": "모델 편향성·공정성 검증 리포트",             "model": "opus",   "when": ["ai_model", "vision", "prediction", "llm", "chatbot"]},
    ]},

    # ══════════════════════════════════════════════════════════════════════
    # DELIVER (납품·운영) — "전달하고 운영하기"
    # ══════════════════════════════════════════════════════════════════════
    {"phase": "DELIVER", "nodes": [
        # 고객 납품
        {"name": "배포 계획서 (Deployment Plan)",              "model": "sonnet", "when": "always"},
        {"name": "데이터 이행 계획서 (Migration Plan)",        "model": "sonnet", "when": "always"},
        {"name": "사용자 매뉴얼",                              "model": "sonnet", "when": "always"},
        {"name": "관리자 매뉴얼",                              "model": "sonnet", "when": "always"},
        {"name": "최종 검수 리포트",                           "model": "opus",   "when": "always"},
        # 내부
        {"name": "배포 체크리스트",                            "model": "sonnet", "when": "always"},
        {"name": "롤백 계획서",                                "model": "sonnet", "when": "always"},
        {"name": "운영 가이드 (모니터링·알림)",                "model": "sonnet", "when": "always"},
        {"name": "프로젝트 완료 보고서",                       "model": "opus",   "when": "always"},
    ]},
]


# ---------------------------------------------------------------------------
# Phase 내 노드 순서 의존성
# ---------------------------------------------------------------------------
# (from_node_name, to_node_name) — 두 노드 모두 존재할 때만 엣지 생성

INTRA_PHASE_DEPS: list[tuple[str, str]] = [
    # ── DEFINE ──
    ("프로젝트 착수 보고서",                   "프로젝트 관리 계획서 (PMP)"),
    ("AS-IS 현행 시스템 분석서",               "TO-BE 목표 시스템 정의서"),
    ("TO-BE 목표 시스템 정의서",               "GAP 분석서"),
    ("GAP 분석서",                             "기능 백로그 (Product Backlog)"),
    ("PRD (제품 요구사항 정의서)",             "기능 백로그 (Product Backlog)"),
    ("기능 백로그 (Product Backlog)",           "사용자 흐름도 (User Flow)"),
    ("기능 백로그 (Product Backlog)",           "서비스 운영 정책서 (SLA·과금·보안)"),
    ("현황 진단 리포트",                       "PoC 설계서"),
    ("PoC 설계서",                             "기대효과 분석 (ROI)"),

    # ── DESIGN ── (HLD 이후)
    ("시스템 아키텍처 설계서 (HLD)",           "IA (정보 구조도)"),
    ("시스템 아키텍처 설계서 (HLD)",           "API 설계서"),
    ("시스템 아키텍처 설계서 (HLD)",           "DB 설계서 (ERD·테이블 정의)"),
    ("시스템 아키텍처 설계서 (HLD)",           "보안 설계서 (ISMS)"),
    ("시스템 아키텍처 설계서 (HLD)",           "모델 아키텍처 설계서"),
    ("IA (정보 구조도)",                       "화면 목록 정의서"),
    ("화면 목록 정의서",                       "화면 설계서 (와이어프레임+스토리보드)"),
    ("화면 설계서 (와이어프레임+스토리보드)",   "컴포넌트 정의서 (디자인 시스템)"),
    ("화면 설계서 (와이어프레임+스토리보드)",   "상태 정의서 (State Definition)"),
    # v10 컴포넌트 조합 체인: 화면설계서 → 토큰 → 라이브러리 → 레지스트리 → 레시피 → 조립 → UI디자인시안
    ("컴포넌트 정의서 (디자인 시스템)",         "디자인 토큰"),
    ("화면 설계서 (와이어프레임+스토리보드)",   "디자인 토큰"),
    ("디자인 토큰",                             "컴포넌트 라이브러리"),
    ("컴포넌트 라이브러리",                     "컴포넌트 레지스트리"),
    ("화면 설계서 (와이어프레임+스토리보드)",   "컴포넌트 레지스트리"),
    ("컴포넌트 레지스트리",                     "페이지 레시피"),
    ("페이지 레시피",                           "페이지 조립"),
    ("페이지 조립",                             "UI 디자인 시안"),
    ("컴포넌트 라이브러리",                     "UI 디자인 시안"),
    ("디자인 토큰",                             "UI 디자인 시안"),
    ("화면 설계서 (와이어프레임+스토리보드)",   "UI 디자인 시안"),
    ("API 설계서",                             "인터페이스 명세서 (ICD)"),
    ("시스템 통합 아키텍처 설계",              "데이터 매핑 테이블"),

    # ── BUILD ── (개발표준 이후)
    ("개발표준 정의서 (코드 컨벤션·브랜치 전략)", "프론트엔드 공통 인프라"),
    ("프론트엔드 공통 인프라",                     "프론트엔드 컴포넌트 구현"),
    ("개발표준 정의서 (코드 컨벤션·브랜치 전략)", "백엔드 API 구현"),
    ("개발표준 정의서 (코드 컨벤션·브랜치 전략)", "DB 스키마 및 마이그레이션 구현"),
    # AI 모델 체인
    ("탐색적 데이터 분석 (EDA) 리포트",        "모델 선정 보고서"),
    ("모델 선정 보고서",                       "데이터 전처리 파이프라인"),
    ("데이터 전처리 파이프라인",               "모델 학습 스크립트"),
    ("모델 학습 스크립트",                     "모델 성능 평가 리포트"),
    # 비전
    ("비전 모델 아키텍처 설계",                "학습·추론 파이프라인 구현"),
    # 예측
    ("예측 모델 개발",                         "이상감지 임계값 최적화"),
    # LLM
    ("LLM 프롬프트 엔지니어링 설계서",         "RAG 파이프라인 구축"),
    ("RAG 파이프라인 구축",                    "LLM 서비스 API 개발"),
    # 챗봇
    ("챗봇 시나리오 설계서",                   "대화 흐름 구현"),
    # 데이터 파이프라인
    ("데이터 수집 파이프라인 설계",            "데이터 정제·변환 (ETL/ELT) 구현"),
    # MLOps
    ("MLOps 환경 구성",                        "모델 서빙 API 배포"),

    # ── VERIFY ── (테스트 계획 → 실행 → 결과)
    ("테스트 계획서",                          "테스트 시나리오"),
    ("테스트 시나리오",                        "단위 테스트 결과"),
    ("테스트 시나리오",                        "UAT 시나리오 및 결과"),
    ("단위 테스트 결과",                       "통합 테스트 결과"),
    ("통합 테스트 결과",                       "성능 테스트 결과"),
    ("통합 테스트 결과",                       "보안 점검 결과"),
    ("성능 테스트 결과",                       "테스트 결과 보고서"),
    ("보안 점검 결과",                         "테스트 결과 보고서"),
    ("UAT 시나리오 및 결과",                   "테스트 결과 보고서"),
    ("테스트 결과 보고서",                     "결함 리스트"),

    # ── DELIVER ── (배포 → 매뉴얼 → 검수 → 완료보고서)
    ("배포 계획서 (Deployment Plan)",           "데이터 이행 계획서 (Migration Plan)"),
    ("배포 계획서 (Deployment Plan)",           "배포 체크리스트"),
    ("배포 계획서 (Deployment Plan)",           "롤백 계획서"),
    ("배포 체크리스트",                        "운영 가이드 (모니터링·알림)"),
    ("사용자 매뉴얼",                          "프로젝트 완료 보고서"),
    ("관리자 매뉴얼",                          "프로젝트 완료 보고서"),
    ("최종 검수 리포트",                       "프로젝트 완료 보고서"),
    ("운영 가이드 (모니터링·알림)",            "프로젝트 완료 보고서"),
]


# ---------------------------------------------------------------------------
# 결과 타입 (인터페이스 불변)
# ---------------------------------------------------------------------------

@dataclass
class IntakeResult:
    engagement_id: str
    project_ids: list[str] = field(default_factory=list)
    node_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# IntakeProcessor
# ---------------------------------------------------------------------------

class IntakeProcessor:
    """
    AI SI 인테이크 폼 JSON → Engagement + N개 프로젝트 + DAG + 노드 자동 생성.

    설계 불변 조건:
      Rule 9 : TASK 노드마다 QA 노드 자동 쌍 생성
      Gate   : 실제 노드가 있는 인접 Phase 사이에만 삽입
      Schema : 공개 폼(snake_case string) + 내부 폼(snake_case array) + 구 폼(camelCase) 모두 지원
    """

    def __init__(self, db: DatabaseAdapter, created_by: str, ai_adapter: Any = None) -> None:
        self._db = db
        self._created_by = created_by
        self._ai_adapter = ai_adapter

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    async def process(self, submission_id: str) -> IntakeResult:
        """
        intake_submissions 레코드 → Engagement + Projects + DAGs + Nodes.
        성공 시 submission.status = 'CONVERTED'.
        실패 시 submission.status = 'FAILED'.
        """
        row = await self._db.fetchone(
            "SELECT * FROM intake_submissions WHERE id=?", (submission_id,)
        )
        if not row:
            raise ValueError(f"intake_submission 없음: {submission_id}")

        raw = json.loads(row["raw_json"])

        # 레퍼런스 URL 분석 (크롤링 + AI 요약)
        try:
            from engine.intake.reference_analyzer import analyze_references
            _adapter = self._ai_adapter
            if _adapter is None:
                import shutil as _shutil
                _cli = _shutil.which("claude")
                if _cli:
                    from engine.ai.model_adapter import CLIProxyAdapter
                    _adapter = CLIProxyAdapter(_cli)
            ref_results = await analyze_references(raw, adapter=_adapter)
            if ref_results:
                raw["_reference_analysis"] = ref_results
                # raw_json도 업데이트 (분석 결과 영구 보존)
                await self._db.execute(
                    "UPDATE intake_submissions SET raw_json=?, updated_at=? WHERE id=?",
                    (json.dumps(raw, ensure_ascii=False), _now(), submission_id),
                )
                logger.info("reference_analysis_done submission_id=%s urls=%d",
                            submission_id, len(ref_results))
        except Exception as ref_exc:
            logger.warning("reference_analysis_failed submission_id=%s error=%s",
                           submission_id, str(ref_exc))

        # 프로젝트 Plan 자동 생성 (AI 분석 → 방향 수립 + 불필요 노드 식별)
        try:
            from engine.intake.project_planner import generate_project_plan
            _plan_adapter = self._ai_adapter
            if _plan_adapter is None:
                import shutil as _shutil2
                _cli2 = _shutil2.which("claude")
                if _cli2:
                    from engine.ai.model_adapter import CLIProxyAdapter
                    _plan_adapter = CLIProxyAdapter(_cli2)
            plan = await generate_project_plan(raw, adapter=_plan_adapter)
            if plan:
                raw["_project_plan"] = plan
                await self._db.execute(
                    "UPDATE intake_submissions SET raw_json=?, updated_at=? WHERE id=?",
                    (json.dumps(raw, ensure_ascii=False), _now(), submission_id),
                )
                logger.info("project_plan_generated submission_id=%s", submission_id)
        except Exception as plan_exc:
            logger.warning("project_plan_failed submission_id=%s error=%s",
                           submission_id, str(plan_exc))

        await self._update_submission_status(submission_id, "CONVERTING")
        try:
            result = await self._create_engagement_and_projects(submission_id, raw)
            await self._update_submission_status(
                submission_id, "CONVERTED",
                engagement_id=result.engagement_id,
            )
            return result
        except Exception as exc:
            await self._update_submission_status(submission_id, "FAILED")
            logger.error(
                "intake_processing_failed submission_id=%s error=%s",
                submission_id, str(exc),
            )
            raise

    # ──────────────────────────────────────────────────────────────────────
    # 생성 로직
    # ──────────────────────────────────────────────────────────────────────

    async def _create_engagement_and_projects(
        self, submission_id: str, raw: dict
    ) -> IntakeResult:
        now = _now()
        engagement_id = str(uuid.uuid4())
        result = IntakeResult(engagement_id=engagement_id)

        # ── Engagement 생성 ───────────────────────────────────────────────
        eng_name = _resolve(
            raw,
            "project_name",    # 신규 내부·공개 폼 (snake_case)
            "projectName",     # 구 폼 (camelCase)
            default="신규 AI SI 프로젝트",
        )
        client_name = _resolve(
            raw,
            "contact_company", # 내부 폼
            "company_name",    # 공개 폼
            "clientName",      # 구 폼
            default="",
        )
        priority = _resolve(raw, "priority", default=3)

        # S11-A1: 도메인 자동 감지 → global_context 에 저장
        # 키워드 기반 매칭 (LLM 호출 X, 결정적·빠름).
        # 다운스트림 (executor·harness) 가 domain_profile 을 활용해 spec override.
        try:
            from engine.intake.domain_profiles import detect_profile, load_profile
            _combined_text = " ".join(str(v) for v in raw.values() if isinstance(v, (str, int, float)))
            _detected = detect_profile(_combined_text)
            _profile_data = load_profile(_detected) if _detected else None
            if _detected and _profile_data:
                raw["_domain_profile"] = _detected
                raw["_domain_profile_data"] = _profile_data
                logger.info(
                    "domain_profile_detected engagement=%s profile=%s",
                    engagement_id[:8], _detected,
                )
        except Exception as _pe:
            logger.warning("domain_profile_detect_failed: %s", _pe)

        await self._db.execute(
            """INSERT INTO engagements
               (id, name, client_name, intake_submission_id, status,
                global_context, priority, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'INTAKE', ?, ?, ?, ?, ?)""",
            (
                engagement_id,
                eng_name,
                client_name,
                submission_id,
                json.dumps(raw, ensure_ascii=False),
                int(priority) if str(priority).isdigit() else 3,
                self._created_by, now, now,
            ),
        )

        # V10: Phase 예산 동적 스케일링 (Level 1 Intake Pre-scale)
        # SizeProfile 추출 → phase별 예산 override 계산 → engagements 업데이트
        try:
            from engine.intake.size_estimator import estimate_size
            from engine.core.budget_scaler import scale_engagement_budget, save_override
            _profile = estimate_size(raw)
            _override = scale_engagement_budget(_profile)
            await save_override(self._db, engagement_id, _override)
            logger.info(
                "v10_budget_override_applied engagement=%s type=%s override=%s",
                engagement_id[:8], _profile.project_type,
                {p: f"{v//1000}K" for p, v in _override.items()},
            )
        except Exception as _bs_err:
            # 스케일러 실패해도 engagement 생성은 계속 (기본값으로 fallback)
            logger.warning(
                "v10_budget_scaler_skipped engagement=%s error=%s",
                engagement_id[:8], _bs_err,
            )

        # ── 컴포넌트 결정 → 프로젝트/DAG/노드 생성 ──────────────────────
        components = self._determine_components(raw)

        # Plan에서 제외 노드 추출
        plan = raw.get("_project_plan", {})
        exclude_names = set(plan.get("exclude_nodes", []))

        for comp in components:
            project_id = await self._create_project(
                engagement_id, submission_id, comp, raw, now
            )
            # 환경설정 자동 생성
            await generate_env_defaults(
                self._db, project_id, engagement_id,
                comp["type"], raw, self._created_by,
            )
            node_count = await self._create_dag_and_nodes(
                project_id, engagement_id, comp, raw,
                exclude_names=exclude_names or None,
            )
            result.project_ids.append(project_id)
            result.node_counts[project_id] = node_count

        logger.info(
            "intake_processed engagement_id=%s project_count=%d total_nodes=%d",
            engagement_id, len(result.project_ids), sum(result.node_counts.values()),
        )
        return result

    # ──────────────────────────────────────────────────────────────────────
    # 컴포넌트 구조 결정 (v9 AI SI 도메인)
    # ──────────────────────────────────────────────────────────────────────

    def _determine_components(self, raw: dict) -> list[dict]:
        """
        AI SI 컴포넌트 분리 기준:
          MASTER  : 항상 생성 (전체 프로젝트 코디네이션)
          SI      : scope에 "si" 포함 시 — 시스템 통합은 별도 납품 스트림
          MLOPS   : scope에 "mlops" 포함 시 — 인프라·운영은 별도 스트림
          DATA    : scope에 "data_pipeline" 포함이고 mlops는 없을 때

        나머지 범위(ai_model / llm / vision / prediction / chatbot / consulting)는
        MASTER 컴포넌트 내 노드로 생성.
        """
        scopes = _resolve_list(raw, "scope", "project_types", "projectTypes")
        proj_name = _resolve(
            raw,
            "project_name", "projectName",
            default="신규 AI SI 프로젝트",
        )

        components: list[dict] = [{"type": "MASTER", "name": proj_name}]

        if "si" in scopes:
            components.append({"type": "SI", "name": f"{proj_name} 시스템통합"})

        if "mlops" in scopes:
            components.append({"type": "MLOPS", "name": f"{proj_name} MLOps"})
        elif "data_pipeline" in scopes:
            # data_pipeline만 있으면 DATA 컴포넌트로 분리
            components.append({"type": "DATA", "name": f"{proj_name} 데이터파이프라인"})

        return components

    # ──────────────────────────────────────────────────────────────────────
    # 프로젝트 INSERT
    # ──────────────────────────────────────────────────────────────────────

    async def _create_project(
        self,
        engagement_id: str,
        submission_id: str,
        comp: dict,
        raw: dict,
        now: str,
    ) -> str:
        project_id = str(uuid.uuid4())
        client_name = _resolve(
            raw,
            "contact_company", "company_name", "clientName",
            default="",
        )
        # project_type: scope 첫 번째 값 또는 component type 기반
        scopes = _resolve_list(raw, "scope", "project_types", "projectTypes")
        project_type = scopes[0] if scopes else comp["type"].lower()

        priority = _resolve(raw, "priority", default=3)

        await self._db.execute(
            """INSERT INTO projects
               (id, name, client_name, project_type, status, global_context,
                intake_submission_id, engagement_id, component_type,
                priority, phase, created_by, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'INTAKE', ?, ?, ?, ?, ?, 'PLANNING', ?, ?, ?, 0)""",
            (
                project_id,
                comp["name"],
                client_name,
                project_type,
                json.dumps(raw, ensure_ascii=False),
                submission_id,
                engagement_id,
                comp["type"],
                int(priority) if str(priority).isdigit() else 3,
                self._created_by, now, now,
            ),
        )
        return project_id

    # ──────────────────────────────────────────────────────────────────────
    # DAG + 노드 생성
    # ──────────────────────────────────────────────────────────────────────

    async def _create_dag_and_nodes(
        self,
        project_id: str,
        engagement_id: str,
        comp: dict,
        raw: dict,
        exclude_names: set | None = None,
    ) -> int:
        now = _now()
        dag_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO dags
               (id, project_id, status, created_at, updated_at, version)
               VALUES (?, ?, 'INITIALIZING', ?, ?, 0)""",
            (dag_id, project_id, now, now),
        )

        # 프로젝트 유형 판별 (신규 vs 고도화)
        project_type = raw.get("project_type", raw.get("projectType", "new"))
        is_new = project_type in ("new", "신규", "신규 구축", "")
        is_upgrade = project_type in ("upgrade", "고도화", "리뉴얼", "enhancement")
        scopes = set(_resolve_list(raw, "scope", "project_types", "projectTypes"))

        def _is_applicable(when) -> bool:
            """when 조건 평가 → True면 실행 대상, False면 SKIPPED."""
            if when == "always":
                return True
            if when == "new":
                return is_new
            if when == "upgrade":
                return is_upgrade
            if isinstance(when, list):
                return bool(set(when) & scopes)
            return True

        total_nodes = 0
        name_to_id: dict[str, str] = {}
        skipped_names: set[str] = set()
        phases_with_nodes: set[str] = set()

        for tmpl in NODE_TEMPLATES:
            phase = tmpl["phase"]
            for node_spec in tmpl["nodes"]:
                applicable = _is_applicable(node_spec.get("when", "always"))
                # exclude_names로 명시적 제외된 산출물도 SKIPPED
                if exclude_names and node_spec["name"] in exclude_names:
                    applicable = False
                initial_state = "NOT_STARTED" if applicable else "SKIPPED"

                # TASK 노드
                task_id = str(uuid.uuid4())
                model = MODEL_MAP.get(node_spec["model"], MODEL_MAP["sonnet"])
                await self._insert_node(
                    dag_id, project_id, task_id,
                    "TASK", phase, node_spec["name"], model, now,
                    initial_state=initial_state,
                )
                name_to_id[node_spec["name"]] = task_id
                if not applicable:
                    skipped_names.add(node_spec["name"])
                phases_with_nodes.add(phase)
                total_nodes += 1

                # Rule 9: QA 노드 자동 쌍 생성
                qa_id = str(uuid.uuid4())
                await self._insert_node(
                    dag_id, project_id, qa_id,
                    "QA", phase, f"[QA] {node_spec['name']}", model, now,
                    task_pair_node_id=task_id,
                    initial_state=initial_state,
                )
                await self._db.execute(
                    "UPDATE nodes SET qa_pair_node_id=? WHERE id=?",
                    (qa_id, task_id),
                )
                name_to_id[f"[QA] {node_spec['name']}"] = qa_id
                phases_with_nodes.add(phase)
                total_nodes += 1

                # TASK → QA 엣지
                await self._insert_edge(dag_id, task_id, qa_id, "QA_PAIR", now)

        # Phase 내 순서 의존성 엣지
        # QA→TASK 의존: 선행 TASK의 QA가 통과해야 후행 TASK 실행 가능
        # (TASK→TASK면 QA 통과 전에 후행이 실행되는 race condition 발생)
        for from_name, to_name in INTRA_PHASE_DEPS:
            if from_name in name_to_id and to_name in name_to_id:
                # from의 QA 노드 ID를 사용 (QA 통과 후에만 다음 TASK 실행)
                from_qa_name = f"[QA] {from_name}"
                from_id = name_to_id.get(from_qa_name, name_to_id[from_name])
                await self._insert_edge(
                    dag_id, from_id, name_to_id[to_name],
                    "DEPENDS_ON", now,
                )

        # Phase 간 Gate 노드 삽입 (노드가 실제로 있는 Phase 사이에만)
        gate_count = await self._insert_phase_gates(
            dag_id, project_id, name_to_id, phases_with_nodes, now, raw=raw,
        )
        total_nodes += gate_count

        # DAG 완료 처리
        await self._db.execute(
            "UPDATE dags SET total_nodes=?, status='VALID', updated_at=? WHERE id=?",
            (total_nodes, now, dag_id),
        )
        return total_nodes

    # ──────────────────────────────────────────────────────────────────────
    # Phase Gate 삽입 (v9: 실제 노드가 있는 Phase 사이에만)
    # ──────────────────────────────────────────────────────────────────────

    async def _insert_phase_gates(
        self,
        dag_id: str,
        project_id: str,
        name_to_id: dict[str, str],
        phases_with_nodes: set[str],
        now: str,
        raw: dict | None = None,
    ) -> int:
        """
        PHASE_ORDER 기준으로 인접한 두 Phase가 모두 실제 노드를 가질 때만 Gate 삽입.
        Edge 연결: 현재 phase의 모든 QA 노드 → GATE → 다음 phase의 모든 TASK 노드.
        반환: 생성된 Gate 노드 수.
        """
        from engine.core.state_machine import PHASE_ORDER

        gate_count = 0
        ordered_active = [p for p in PHASE_ORDER if p in phases_with_nodes]

        # phase별 노드 분류 (GATE edge 연결용)
        phase_nodes: dict[str, list[tuple[str, str]]] = {}  # phase → [(node_id, node_type)]
        for name, node_id in name_to_id.items():
            # name_to_id에서 phase 정보 추출: QA는 "[QA] " 접두사, GATE는 "[GATE]" 접두사
            if name.startswith("[GATE]"):
                continue
            row = await self._db.fetchone(
                "SELECT phase, node_type FROM nodes WHERE id=?", (node_id,)
            )
            if row:
                phase_nodes.setdefault(row["phase"], []).append(
                    (node_id, row["node_type"])
                )

        for i, phase in enumerate(ordered_active[:-1]):
            next_phase = ordered_active[i + 1]
            gate_id = str(uuid.uuid4())
            gate_name = f"[GATE] {phase} → {next_phase}"
            # BUILD→VERIFY: 인테이크 옵션에 따라 자동/수동 승인
            # "사람 확인" (기본값) → auto_approve=0 → 앱 미리보기 후 승인
            # "자동 진행" → auto_approve=1 → 기존처럼 자동 승인
            if phase == "BUILD" and next_phase == "VERIFY":
                _review_mode = (raw or {}).get("review_mode", "human")  # 기본: 사람 확인
                auto_approve = 1 if _review_mode == "auto" else 0
            else:
                auto_approve = 0
            await self._db.execute(
                """INSERT INTO nodes
                   (id, dag_id, project_id, node_type, phase, name,
                    state, gate_auto_approve, created_at, updated_at, version)
                   VALUES (?, ?, ?, 'GATE', ?, ?, 'NOT_STARTED', ?, ?, ?, 0)""",
                (gate_id, dag_id, project_id, phase, gate_name, auto_approve, now, now),
            )
            name_to_id[gate_name] = gate_id
            gate_count += 1

            # Edge: 현재 phase의 QA 노드 → GATE (QA가 없으면 TASK → GATE)
            current_nodes = phase_nodes.get(phase, [])
            qa_nodes = [(nid, nt) for nid, nt in current_nodes if nt == "QA"]
            if qa_nodes:
                for nid, _ in qa_nodes:
                    await self._insert_edge(dag_id, nid, gate_id, "GATE_WAIT", now)
            else:
                # QA 없으면 TASK → GATE
                for nid, nt in current_nodes:
                    if nt == "TASK":
                        await self._insert_edge(dag_id, nid, gate_id, "GATE_WAIT", now)

            # Edge: GATE → 다음 phase의 모든 TASK 노드
            next_nodes = phase_nodes.get(next_phase, [])
            for nid, nt in next_nodes:
                if nt == "TASK":
                    await self._insert_edge(dag_id, gate_id, nid, "GATE_UNLOCK", now)

        return gate_count

    # ──────────────────────────────────────────────────────────────────────
    # DB INSERT 헬퍼
    # ──────────────────────────────────────────────────────────────────────

    async def _insert_node(
        self,
        dag_id: str, project_id: str, node_id: str,
        node_type: str, phase: str, name: str, model: str, now: str,
        task_pair_node_id: str | None = None,
        initial_state: str = "NOT_STARTED",
    ) -> None:
        await self._db.execute(
            """INSERT INTO nodes
               (id, dag_id, project_id, node_type, phase, name,
                state, assigned_model, task_pair_node_id,
                created_at, updated_at, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                node_id, dag_id, project_id, node_type, phase, name,
                initial_state, model, task_pair_node_id, now, now,
            ),
        )

    async def _insert_edge(
        self,
        dag_id: str, from_id: str, to_id: str,
        edge_type: str, now: str,
    ) -> None:
        edge_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT OR IGNORE INTO edges
               (id, dag_id, from_node_id, to_node_id, edge_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (edge_id, dag_id, from_id, to_id, edge_type, now),
        )

    async def _update_submission_status(
        self,
        submission_id: str,
        status: str,
        engagement_id: str | None = None,
    ) -> None:
        if engagement_id:
            await self._db.execute(
                """UPDATE intake_submissions
                   SET status=?, engagement_id=?, updated_at=?
                   WHERE id=?""",
                (status, engagement_id, _now(), submission_id),
            )
        else:
            await self._db.execute(
                "UPDATE intake_submissions SET status=?, updated_at=? WHERE id=?",
                (status, _now(), submission_id),
            )


# ---------------------------------------------------------------------------
# 중복 노드 정리 유틸리티
# ---------------------------------------------------------------------------

async def deduplicate_dag_nodes(db, dag_id: str) -> dict:
    """DAG 내 중복 노드 정리. 동일 (name, phase, node_type)이면 최신 1개만 유지.

    중복 원인: 동일 engagement 재처리, processor 재실행 등.
    나머지는 SKIPPED 처리 + 관련 edge 비활성화.

    Returns:
        {"deduped": int, "kept": int, "details": [...]}
    """
    rows = await db.fetchall(
        """SELECT id, name, phase, node_type, state, created_at
           FROM nodes WHERE dag_id=? ORDER BY name, created_at DESC""",
        (dag_id,),
    )

    # (name, phase, node_type) 그룹별로 최신 1개만 유지
    seen: dict[tuple, str] = {}  # key → kept node_id
    duplicates: list[dict] = []

    for r in rows:
        key = (r["name"], r["phase"], r["node_type"])
        if key not in seen:
            seen[key] = r["id"]
        else:
            duplicates.append({
                "id": r["id"],
                "name": r["name"],
                "phase": r["phase"],
                "node_type": r["node_type"],
                "state": r["state"],
                "kept_id": seen[key],
            })

    now = _now()
    for dup in duplicates:
        # 중복 노드 SKIPPED 처리
        await db.execute(
            "UPDATE nodes SET state='SKIPPED', updated_at=? WHERE id=?",
            (now, dup["id"]),
        )
        # 관련 edge 비활성화
        await db.execute(
            "UPDATE edges SET is_active=0 WHERE from_node_id=? OR to_node_id=?",
            (dup["id"], dup["id"]),
        )

    return {
        "deduped": len(duplicates),
        "kept": len(seen),
        "details": duplicates[:20],
    }


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
