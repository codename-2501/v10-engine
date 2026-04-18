"""
engine/core/env_config_generator.py
환경설정 자동 생성 — 프로젝트 생성 시 컴포넌트/서비스 유형에 따라 기본 환경변수 템플릿 자동 등록.

흐름:
  IntakeProcessor._create_project() 완료 직후
  → generate_env_defaults(db, project_id, engagement_id, component_type, raw)
  → project_env_vars 테이블에 기본 키 + placeholder 값 INSERT
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from engine.db.adapter import DatabaseAdapter
from engine.security.crypto import AES256GCM

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 컴포넌트별 기본 환경변수 템플릿
# ---------------------------------------------------------------------------

# 공통 (모든 컴포넌트)
_COMMON_VARS: list[dict[str, Any]] = [
    {"key": "GITHUB_TOKEN",       "description": "GitHub 저장소 접근 토큰",           "is_secret": True},
    {"key": "DEPLOYMENT_TARGET",  "description": "배포 대상 환경 (staging/production)", "is_secret": False},
    {"key": "PROJECT_NAME",       "description": "프로젝트 표시명",                   "is_secret": False},
]

# 서비스 유형(service_type)별 추가 변수
_SERVICE_TYPE_VARS: dict[str, list[dict[str, Any]]] = {
    "web_service": [
        {"key": "WEB_DOMAIN",          "description": "웹 서비스 도메인",         "is_secret": False},
        {"key": "SSL_CERT_PATH",       "description": "SSL 인증서 경로",         "is_secret": False},
        {"key": "CDN_URL",             "description": "CDN 엔드포인트 URL",      "is_secret": False},
    ],
    "mobile_app": [
        {"key": "APP_BUNDLE_ID",       "description": "앱 번들 ID (iOS/Android)",  "is_secret": False},
        {"key": "PUSH_NOTIFICATION_KEY", "description": "푸시 알림 서버 키",       "is_secret": True},
        {"key": "APP_STORE_API_KEY",   "description": "앱 스토어 배포 API 키",     "is_secret": True},
    ],
    "admin_backoffice": [
        {"key": "ADMIN_DOMAIN",        "description": "관리자 백오피스 도메인",    "is_secret": False},
        {"key": "ADMIN_JWT_SECRET",    "description": "관리자 JWT 비밀 키",       "is_secret": True},
    ],
}

# scope(기능 범위)별 추가 변수
_SCOPE_VARS: dict[str, list[dict[str, Any]]] = {
    "프론트엔드": [
        {"key": "NEXT_PUBLIC_API_URL", "description": "프론트엔드 API 베이스 URL", "is_secret": False},
    ],
    "백엔드": [
        {"key": "DATABASE_URL",        "description": "메인 DB 연결 문자열",       "is_secret": True},
        {"key": "REDIS_URL",           "description": "Redis 캐시 연결 URL",      "is_secret": True},
        {"key": "API_SECRET_KEY",      "description": "API 서버 시크릿 키",        "is_secret": True},
    ],
    "결제시스템": [
        {"key": "PAYMENT_API_KEY",     "description": "PG사 API 키",             "is_secret": True},
        {"key": "PAYMENT_SECRET_KEY",  "description": "PG사 시크릿 키",           "is_secret": True},
        {"key": "PAYMENT_WEBHOOK_URL", "description": "결제 웹훅 수신 URL",       "is_secret": False},
    ],
    "채팅": [
        {"key": "WEBSOCKET_URL",       "description": "WebSocket 서버 URL",      "is_secret": False},
        {"key": "CHAT_STORAGE_BUCKET", "description": "채팅 미디어 저장소 버킷",    "is_secret": False},
    ],
}

# AI 관련 변수 (need_ai=yes)
_AI_VARS: list[dict[str, Any]] = [
    {"key": "ANTHROPIC_API_KEY",   "description": "Claude API 키",             "is_secret": True},
    {"key": "AI_MODEL_ID",         "description": "기본 AI 모델 ID",            "is_secret": False},
]

# 실시간 기능 변수 (need_realtime=yes)
_REALTIME_VARS: list[dict[str, Any]] = [
    {"key": "REALTIME_WS_PORT",    "description": "실시간 WebSocket 포트",      "is_secret": False},
]

# 외부 연동 변수 (need_integration=yes) — 기본
_INTEGRATION_VARS: list[dict[str, Any]] = [
    {"key": "OAUTH_CLIENT_ID",     "description": "소셜 로그인 OAuth Client ID",  "is_secret": False},
    {"key": "OAUTH_CLIENT_SECRET", "description": "소셜 로그인 OAuth Secret",     "is_secret": True},
]

# ---------------------------------------------------------------------------
# features 키워드 → 필요 환경변수 매핑
# ---------------------------------------------------------------------------

_FEATURE_KEYWORD_VARS: list[tuple[list[str], list[dict[str, Any]]]] = [
    # 소셜 로그인
    (["소셜", "로그인", "회원가입"], [
        {"key": "GOOGLE_CLIENT_ID",        "description": "Google OAuth Client ID",           "is_secret": False},
        {"key": "GOOGLE_CLIENT_SECRET",    "description": "Google OAuth Client Secret",       "is_secret": True},
        {"key": "KAKAO_REST_API_KEY",      "description": "Kakao REST API 키",               "is_secret": True},
        {"key": "KAKAO_CLIENT_SECRET",     "description": "Kakao Client Secret",              "is_secret": True},
        {"key": "NAVER_CLIENT_ID",         "description": "Naver Login Client ID",            "is_secret": False},
        {"key": "NAVER_CLIENT_SECRET",     "description": "Naver Login Client Secret",        "is_secret": True},
        {"key": "APPLE_SERVICE_ID",        "description": "Apple Sign-In Service ID",         "is_secret": False},
        {"key": "APPLE_TEAM_ID",           "description": "Apple Developer Team ID",          "is_secret": False},
        {"key": "APPLE_KEY_ID",            "description": "Apple Sign-In Key ID",             "is_secret": False},
        {"key": "APPLE_PRIVATE_KEY",       "description": "Apple Sign-In Private Key (.p8)",  "is_secret": True},
    ]),
    # 지도 / 위치 기반
    (["지도", "검색", "위치", "병원"], [
        {"key": "GOOGLE_MAPS_API_KEY",     "description": "Google Maps JavaScript/Places API 키", "is_secret": True},
        {"key": "KAKAO_JAVASCRIPT_KEY",    "description": "Kakao Map JavaScript 앱 키",       "is_secret": False},
    ]),
    # 파일 업로드 / 이미지 저장
    (["사진", "이미지", "파일", "업로드", "프로필"], [
        {"key": "AWS_ACCESS_KEY_ID",       "description": "AWS S3 액세스 키 ID",              "is_secret": True},
        {"key": "AWS_SECRET_ACCESS_KEY",   "description": "AWS S3 시크릿 액세스 키",           "is_secret": True},
        {"key": "AWS_S3_BUCKET",           "description": "S3 파일 저장 버킷명",               "is_secret": False},
        {"key": "AWS_S3_REGION",           "description": "S3 버킷 리전",                     "is_secret": False},
        {"key": "CDN_DOMAIN",              "description": "이미지 CDN 도메인 (CloudFront 등)", "is_secret": False},
    ]),
    # 푸시 알림
    (["푸시", "알림", "스케줄"], [
        {"key": "FCM_SERVER_KEY",          "description": "Firebase Cloud Messaging 서버 키",  "is_secret": True},
        {"key": "FCM_PROJECT_ID",          "description": "Firebase 프로젝트 ID",              "is_secret": False},
        {"key": "FIREBASE_SERVICE_ACCOUNT", "description": "Firebase 서비스 계정 JSON 키",     "is_secret": True},
    ]),
    # SMS 인증
    (["인증", "본인확인", "SMS", "전화"], [
        {"key": "SMS_API_KEY",             "description": "SMS 발송 API 키 (CoolSMS/NHN)",    "is_secret": True},
        {"key": "SMS_API_SECRET",          "description": "SMS 발송 API Secret",              "is_secret": True},
        {"key": "SMS_SENDER_NUMBER",       "description": "SMS 발신번호",                     "is_secret": False},
    ]),
    # 이메일 발송
    (["이메일", "메일", "발송"], [
        {"key": "SMTP_HOST",               "description": "SMTP 서버 호스트",                 "is_secret": False},
        {"key": "SMTP_PORT",               "description": "SMTP 포트",                       "is_secret": False},
        {"key": "SMTP_USERNAME",           "description": "SMTP 사용자명",                    "is_secret": False},
        {"key": "SMTP_PASSWORD",           "description": "SMTP 비밀번호",                    "is_secret": True},
        {"key": "EMAIL_FROM_ADDRESS",      "description": "발신 이메일 주소",                  "is_secret": False},
    ]),
    # 결제 (구체적 PG사)
    (["결제", "PG", "포인트", "쿠폰"], [
        {"key": "PORTONE_API_KEY",         "description": "포트원(아임포트) API 키",           "is_secret": True},
        {"key": "PORTONE_API_SECRET",      "description": "포트원 API Secret",               "is_secret": True},
        {"key": "PORTONE_MERCHANT_ID",     "description": "포트원 가맹점 식별코드",            "is_secret": False},
        {"key": "TOSS_SECRET_KEY",         "description": "토스페이먼츠 시크릿 키",            "is_secret": True},
        {"key": "TOSS_CLIENT_KEY",         "description": "토스페이먼츠 클라이언트 키",         "is_secret": False},
    ]),
]

# 컴포넌트 유형별 추가 변수
_COMPONENT_VARS: dict[str, list[dict[str, Any]]] = {
    "SI": [
        {"key": "SI_SYSTEM_ENDPOINT",  "description": "통합 대상 시스템 엔드포인트", "is_secret": False},
        {"key": "SI_AUTH_METHOD",      "description": "통합 인증 방식 (oauth/apikey/basic)", "is_secret": False},
    ],
    "MLOPS": [
        {"key": "MODEL_REGISTRY_URL",  "description": "모델 레지스트리 URL",       "is_secret": False},
        {"key": "ARTIFACT_STORAGE_PATH", "description": "모델 아티팩트 저장 경로",  "is_secret": False},
    ],
    "DATA": [
        {"key": "DATA_SOURCE_URL",     "description": "데이터 소스 연결 URL",      "is_secret": True},
        {"key": "DATA_WAREHOUSE_URL",  "description": "데이터 웨어하우스 URL",     "is_secret": True},
    ],
}

# SaaS 배포 변수
_DEPLOY_VARS: dict[str, list[dict[str, Any]]] = {
    "cloud_saas": [
        {"key": "CLOUD_PROVIDER",      "description": "클라우드 프로바이더 (aws/gcp/azure)", "is_secret": False},
        {"key": "CLOUD_REGION",        "description": "배포 리전",                "is_secret": False},
    ],
    "on_premise": [
        {"key": "ON_PREM_HOST",        "description": "온프레미스 서버 호스트",     "is_secret": False},
        {"key": "ON_PREM_SSH_KEY",     "description": "서버 접근 SSH 키",         "is_secret": True},
    ],
}


# ---------------------------------------------------------------------------
# 메인 생성 함수
# ---------------------------------------------------------------------------

async def generate_env_defaults(
    db: DatabaseAdapter,
    project_id: str,
    engagement_id: str,
    component_type: str,
    raw: dict,
    created_by: str,
) -> list[str]:
    """
    인테이크 폼(raw) 분석 → 필요한 환경변수 키를 project_env_vars에 자동 등록.

    반환: 생성된 환경변수 키 목록.
    """
    vars_to_create: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _add(var_list: list[dict[str, Any]]) -> None:
        for v in var_list:
            if v["key"] not in seen_keys:
                seen_keys.add(v["key"])
                vars_to_create.append(v)

    # 1) 공통
    _add(_COMMON_VARS)

    # 2) 컴포넌트별
    if component_type in _COMPONENT_VARS:
        _add(_COMPONENT_VARS[component_type])

    # 3) 서비스 유형별
    service_types = raw.get("service_type", [])
    if isinstance(service_types, str):
        service_types = [service_types]
    for st in service_types:
        if st in _SERVICE_TYPE_VARS:
            _add(_SERVICE_TYPE_VARS[st])

    # 4) 기능 범위별
    scopes = raw.get("scope", [])
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.split(",")]
    for s in scopes:
        if s in _SCOPE_VARS:
            _add(_SCOPE_VARS[s])

    # 5) AI / 실시간 / 외부연동
    if raw.get("need_ai") == "yes":
        _add(_AI_VARS)
    if raw.get("need_realtime") == "yes":
        _add(_REALTIME_VARS)
    if raw.get("need_integration") == "yes":
        _add(_INTEGRATION_VARS)

    # 6) features/use_cases 키워드 분석 → 외부 API 키 자동 매핑
    features_text = " ".join(raw.get("features", []) + raw.get("use_cases", []))
    for keywords, var_list in _FEATURE_KEYWORD_VARS:
        if any(kw in features_text for kw in keywords):
            _add(var_list)

    # 7) 배포 유형
    deploy_types = raw.get("deploy_type", [])
    if isinstance(deploy_types, str):
        deploy_types = [deploy_types]
    for dt in deploy_types:
        if dt in _DEPLOY_VARS:
            _add(_DEPLOY_VARS[dt])

    # ── DB INSERT ─────────────────────────────────────────────────────────
    if not vars_to_create:
        return []

    now = datetime.now(timezone.utc).isoformat()

    # PROJECT_NAME은 실제 값으로 설정
    project_name = raw.get("project_name", raw.get("projectName", ""))

    try:
        encrypt_key = AES256GCM.key_from_env()
    except Exception:
        # 암호화 키가 없으면 평문으로 저장 (개발 환경)
        encrypt_key = None

    created_keys: list[str] = []
    for var in vars_to_create:
        key = var["key"]
        # PROJECT_NAME은 실제 값 설정, 나머지는 placeholder
        if key == "PROJECT_NAME" and project_name:
            value = project_name
        else:
            value = ""  # placeholder — 사용자가 나중에 설정

        if encrypt_key:
            value_encrypted = AES256GCM.encrypt(value, encrypt_key)
        else:
            value_encrypted = value

        await db.execute(
            """INSERT INTO project_env_vars
               (id, scope, scope_id, key, value_encrypted,
                is_secret, is_test_resource, description, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
               ON CONFLICT(scope, scope_id, key) DO NOTHING""",
            (
                str(uuid.uuid4()),
                "PROJECT",
                project_id,
                key,
                value_encrypted,
                1 if var.get("is_secret", True) else 0,
                var.get("description", ""),
                created_by,
                now,
            ),
        )
        created_keys.append(key)

    # Engagement 레벨에도 공유 변수 등록 (중복 시 무시)
    engagement_shared = ["GITHUB_TOKEN", "DEPLOYMENT_TARGET"]
    for key in engagement_shared:
        if key in seen_keys:
            value_encrypted = AES256GCM.encrypt("", encrypt_key) if encrypt_key else ""
            desc = next((v["description"] for v in vars_to_create if v["key"] == key), "")
            await db.execute(
                """INSERT INTO project_env_vars
                   (id, scope, scope_id, key, value_encrypted,
                    is_secret, is_test_resource, description, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
                   ON CONFLICT(scope, scope_id, key) DO NOTHING""",
                (
                    str(uuid.uuid4()),
                    "ENGAGEMENT",
                    engagement_id,
                    key,
                    value_encrypted,
                    desc,
                    created_by,
                    now,
                ),
            )

    logger.info(
        "env_defaults_generated project_id=%s keys=%s",
        project_id, ",".join(created_keys),
    )
    return created_keys


# ---------------------------------------------------------------------------
# DESIGN 산출물 기반 환경변수 자동 추출
# ---------------------------------------------------------------------------

# 외부 API/서비스 키워드 → 환경변수 매핑
_ARTIFACT_API_PATTERNS: list[tuple[list[str], list[dict[str, Any]]]] = [
    # Kakao 서비스
    (["kakao oauth", "카카오 로그인", "카카오 소셜", "kakao login"], [
        {"key": "KAKAO_REST_API_KEY",    "description": "Kakao REST API 키",          "is_secret": True},
        {"key": "KAKAO_CLIENT_SECRET",   "description": "Kakao OAuth Client Secret",  "is_secret": True},
        {"key": "KAKAO_REDIRECT_URI",    "description": "Kakao OAuth 리다이렉트 URI",  "is_secret": False},
    ]),
    (["kakao map", "카카오 지도", "카카오맵"], [
        {"key": "KAKAO_JAVASCRIPT_KEY",  "description": "Kakao Map JavaScript 앱 키",  "is_secret": False},
    ]),
    (["카카오 알림톡", "kakao alimtalk", "알림톡"], [
        {"key": "KAKAO_ALIMTALK_KEY",    "description": "카카오 알림톡 API 키",         "is_secret": True},
        {"key": "KAKAO_SENDER_KEY",      "description": "카카오 발신 프로필 키",         "is_secret": True},
    ]),
    # Firebase / FCM
    (["fcm", "firebase", "푸시", "push notification"], [
        {"key": "FCM_SERVER_KEY",             "description": "Firebase Cloud Messaging 서버 키",  "is_secret": True},
        {"key": "FCM_PROJECT_ID",             "description": "Firebase 프로젝트 ID",              "is_secret": False},
        {"key": "FIREBASE_SERVICE_ACCOUNT",   "description": "Firebase 서비스 계정 JSON",         "is_secret": True},
    ]),
    # Google
    (["google oauth", "구글 로그인", "google login"], [
        {"key": "GOOGLE_CLIENT_ID",      "description": "Google OAuth Client ID",     "is_secret": False},
        {"key": "GOOGLE_CLIENT_SECRET",  "description": "Google OAuth Client Secret", "is_secret": True},
    ]),
    (["google maps", "구글 지도"], [
        {"key": "GOOGLE_MAPS_API_KEY",   "description": "Google Maps API 키",         "is_secret": True},
    ]),
    # Naver
    (["naver login", "네이버 로그인", "naver oauth"], [
        {"key": "NAVER_CLIENT_ID",       "description": "Naver Login Client ID",      "is_secret": False},
        {"key": "NAVER_CLIENT_SECRET",   "description": "Naver Login Client Secret",  "is_secret": True},
    ]),
    # AWS
    (["aws", "s3", "object storage", "오브젝트 스토리지"], [
        {"key": "AWS_ACCESS_KEY_ID",     "description": "AWS/NCP 액세스 키 ID",        "is_secret": True},
        {"key": "AWS_SECRET_ACCESS_KEY", "description": "AWS/NCP 시크릿 액세스 키",     "is_secret": True},
        {"key": "AWS_S3_BUCKET",         "description": "S3/Object Storage 버킷명",    "is_secret": False},
        {"key": "AWS_S3_REGION",         "description": "S3 버킷 리전",               "is_secret": False},
    ]),
    # NCP (네이버 클라우드)
    (["ncp", "네이버 클라우드", "naver cloud"], [
        {"key": "NCP_ACCESS_KEY",        "description": "NCP 액세스 키",              "is_secret": True},
        {"key": "NCP_SECRET_KEY",        "description": "NCP 시크릿 키",              "is_secret": True},
    ]),
    # Claude / Anthropic
    (["claude api", "anthropic", "claude"], [
        {"key": "ANTHROPIC_API_KEY",     "description": "Anthropic Claude API 키",    "is_secret": True},
    ]),
    # OpenAI
    (["openai", "gpt", "whisper"], [
        {"key": "OPENAI_API_KEY",        "description": "OpenAI API 키",              "is_secret": True},
    ]),
    # SMS
    (["sms", "문자", "coolsms", "nhn"], [
        {"key": "SMS_API_KEY",           "description": "SMS 발송 API 키",            "is_secret": True},
        {"key": "SMS_API_SECRET",        "description": "SMS 발송 API Secret",        "is_secret": True},
        {"key": "SMS_SENDER_NUMBER",     "description": "SMS 발신번호",               "is_secret": False},
    ]),
    # 결제
    (["결제", "payment", "pg사", "포트원", "아임포트", "toss"], [
        {"key": "PAYMENT_API_KEY",       "description": "PG사 API 키",               "is_secret": True},
        {"key": "PAYMENT_SECRET_KEY",    "description": "PG사 시크릿 키",             "is_secret": True},
    ]),
    # JWT
    (["jwt", "토큰 인증", "bearer"], [
        {"key": "JWT_SECRET_KEY",        "description": "JWT 서명 비밀 키",            "is_secret": True},
        {"key": "JWT_ALGORITHM",         "description": "JWT 알고리즘 (HS256 등)",     "is_secret": False},
    ]),
    # DB
    (["postgresql", "postgres", "mysql", "mariadb", "database"], [
        {"key": "DATABASE_URL",          "description": "메인 DB 연결 문자열",          "is_secret": True},
    ]),
    # Redis
    (["redis", "캐시", "세션 저장소"], [
        {"key": "REDIS_URL",             "description": "Redis 연결 URL",             "is_secret": True},
    ]),
    # 이메일
    (["smtp", "이메일 발송", "email", "메일"], [
        {"key": "SMTP_HOST",             "description": "SMTP 서버 호스트",            "is_secret": False},
        {"key": "SMTP_PORT",             "description": "SMTP 포트",                  "is_secret": False},
        {"key": "SMTP_USERNAME",         "description": "SMTP 사용자명",               "is_secret": False},
        {"key": "SMTP_PASSWORD",         "description": "SMTP 비밀번호",               "is_secret": True},
    ]),
]


async def extract_env_from_artifacts(
    db: DatabaseAdapter,
    project_id: str,
    engagement_id: str,
    created_by: str = "system",
) -> list[str]:
    """
    DESIGN 산출물에서 외부 API/서비스 키워드를 탐지 → 필요한 환경변수를 자동 등록.

    인테이크 기반 generate_env_defaults와 보완 관계:
    - generate_env_defaults: 프로젝트 생성 시 (인테이크 폼 키워드 기반)
    - extract_env_from_artifacts: DESIGN 완료 시 (산출물 실제 내용 기반)

    중복 키는 ON CONFLICT DO NOTHING으로 무시.
    반환: 새로 생성된 환경변수 키 목록.
    """
    # DESIGN 산출물 로드
    rows = await db.fetchall(
        """SELECT av.storage_path AS content
           FROM nodes n
           JOIN artifacts a ON a.node_id = n.id
           JOIN artifact_versions av ON av.artifact_id = a.id
                AND av.version_num = a.current_version
           WHERE n.project_id=? AND n.phase='DESIGN'
                 AND n.state IN ('COMPLETED', 'BLOCKED', 'INVALID')
                 AND av.storage_path IS NOT NULL""",
        (project_id,),
    )

    if not rows:
        return []

    # 전체 산출물 텍스트 결합 (lowercase)
    combined_text = " ".join(
        (r["content"] or "")[:10000] for r in rows  # 노드당 10K 제한 (메모리 절약)
    ).lower()

    if not combined_text.strip():
        return []

    # 키워드 매칭 → 필요 변수 수집
    vars_to_create: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    # 기존 등록된 키 조회 (중복 방지)
    existing = await db.fetchall(
        "SELECT key FROM project_env_vars WHERE scope='PROJECT' AND scope_id=?",
        (project_id,),
    )
    for e in existing:
        seen_keys.add(e["key"])

    for keywords, var_list in _ARTIFACT_API_PATTERNS:
        if any(kw in combined_text for kw in keywords):
            for v in var_list:
                if v["key"] not in seen_keys:
                    seen_keys.add(v["key"])
                    vars_to_create.append(v)

    if not vars_to_create:
        return []

    # DB INSERT
    now = datetime.now(timezone.utc).isoformat()
    try:
        encrypt_key = AES256GCM.key_from_env()
    except Exception:
        encrypt_key = None

    created_keys: list[str] = []
    for var in vars_to_create:
        value_encrypted = AES256GCM.encrypt("", encrypt_key) if encrypt_key else ""
        await db.execute(
            """INSERT INTO project_env_vars
               (id, scope, scope_id, key, value_encrypted,
                is_secret, is_test_resource, description, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
               ON CONFLICT(scope, scope_id, key) DO NOTHING""",
            (
                str(uuid.uuid4()),
                "PROJECT",
                project_id,
                var["key"],
                value_encrypted,
                1 if var.get("is_secret", True) else 0,
                var.get("description", ""),
                created_by,
                now,
            ),
        )
        created_keys.append(var["key"])

    if created_keys:
        logger.info(
            "env_from_artifacts project_id=%s new_keys=%d keys=%s",
            project_id, len(created_keys), ",".join(created_keys),
        )

    return created_keys


# ---------------------------------------------------------------------------
# 유틸: 프로젝트 환경설정 요약 (API용)
# ---------------------------------------------------------------------------

async def get_env_summary(
    db: DatabaseAdapter,
    project_id: str,
    engagement_id: str,
) -> dict[str, Any]:
    """
    프로젝트에 등록된 환경변수 현황 요약.
    반환: {total, configured, unconfigured, keys: [{key, scope, has_value, description}]}
    """
    rows = await db.fetchall(
        """SELECT key, scope, value_encrypted, is_secret, description
           FROM project_env_vars
           WHERE (scope='PROJECT' AND scope_id=?)
              OR (scope='ENGAGEMENT' AND scope_id=?)
              OR (scope='GLOBAL' AND scope_id='GLOBAL')
           ORDER BY
             CASE scope WHEN 'PROJECT' THEN 1 WHEN 'ENGAGEMENT' THEN 2 ELSE 3 END,
             key""",
        (project_id, engagement_id),
    )

    # 동일 key는 PROJECT 우선 (첫 등장만)
    seen: set[str] = set()
    keys: list[dict] = []
    configured = 0

    for r in rows:
        k = r["key"]
        if k in seen:
            continue
        seen.add(k)
        has_value = bool(r["value_encrypted"] and r["value_encrypted"].strip())
        if has_value:
            # placeholder 빈값 체크 — 암호화된 빈문자열도 has_value=False 처리
            try:
                encrypt_key = AES256GCM.key_from_env()
                decrypted = AES256GCM.decrypt(r["value_encrypted"], encrypt_key)
                has_value = bool(decrypted and decrypted.strip())
            except Exception:
                pass

        if has_value:
            configured += 1

        keys.append({
            "key": k,
            "scope": r["scope"],
            "has_value": has_value,
            "is_secret": bool(r["is_secret"]),
            "description": r["description"] or "",
        })

    return {
        "total": len(keys),
        "configured": configured,
        "unconfigured": len(keys) - configured,
        "keys": keys,
    }
