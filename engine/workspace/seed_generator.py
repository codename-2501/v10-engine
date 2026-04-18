"""
engine/workspace/seed_generator.py
Prisma schema 분석 → 시드 데이터 자동 생성.
모든 프로젝트에서 테스트 계정 + 최소 데이터가 자동으로 들어간다.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import asyncio
from pathlib import Path

logger = logging.getLogger("engine.workspace.seed_generator")

# 모든 프로젝트 공통 테스트 계정
DEFAULT_ACCOUNTS = [
    {"email": "test@test.kr", "password": "Test1234!", "name": "테스트유저", "nickname": "테스터", "role": "USER"},
    {"email": "admin@test.kr", "password": "Test1234!", "name": "관리자", "nickname": "어드민", "role": "ADMIN"},
]


async def generate_seed(workspace_path: Path, stack: dict) -> bool:
    """Prisma schema 분석 → seed.ts 생성 → 실행."""
    be = workspace_path / "backend"

    if stack.get("backend") == "express" and stack.get("orm") == "prisma":
        schema_path = be / "prisma" / "schema.prisma"
        if not schema_path.is_file():
            return False

        models = _parse_prisma_models(schema_path)
        seed_code = _build_seed_code(models)

        seed_path = be / "prisma" / "seed.ts"
        if seed_path.is_file():
            logger.info("seed_already_exists path=%s", seed_path)
            return True

        seed_path.write_text(seed_code, encoding="utf-8")
        logger.info("seed_generated path=%s models=%d", seed_path, len(models))

        # 실행
        result = await asyncio.to_thread(subprocess.run, 
            ["npx", "ts-node", "prisma/seed.ts"],
            cwd=be, capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("seed_executed_ok")
            return True
        else:
            logger.warning("seed_execution_failed stderr=%s", result.stderr[-500:])
            return False

    elif stack.get("backend") == "fastapi":
        return _generate_fastapi_seed(be)

    elif stack.get("backend") == "django":
        return _generate_django_seed(be)

    elif stack.get("backend") in ("spring", "go"):
        return _generate_sql_seed(be)

    return False


def _parse_prisma_models(schema_path: Path) -> list[dict]:
    """Prisma schema에서 모델 구조 추출."""
    content = schema_path.read_text(encoding="utf-8")
    models = []

    model_pattern = re.compile(r"model\s+(\w+)\s*\{([^}]+)\}", re.DOTALL)
    for match in model_pattern.finditer(content):
        name = match.group(1)
        body = match.group(2)

        fields = []
        for line in body.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("@@"):
                continue

            # 필드 파싱: name Type? @default(...) @relation(...)
            field_match = re.match(
                r"(\w+)\s+(String|Int|Float|Boolean|DateTime|Bytes|BigInt|Decimal)(\?)?\s*(.*)",
                line,
            )
            if field_match:
                fname = field_match.group(1)
                ftype = field_match.group(2)
                optional = bool(field_match.group(3))
                rest = field_match.group(4)

                is_id = "@id" in rest
                has_default = "@default" in rest
                is_unique = "@unique" in rest
                is_relation = "@relation" in rest

                default_val = None
                default_match = re.search(r'@default\(([^)]+)\)', rest)
                if default_match:
                    default_val = default_match.group(1)

                fields.append({
                    "name": fname,
                    "type": ftype,
                    "optional": optional,
                    "is_id": is_id,
                    "has_default": has_default,
                    "is_unique": is_unique,
                    "is_relation": is_relation,
                    "default": default_val,
                })

        if fields:
            models.append({"name": name, "fields": fields})

    return models


def _build_seed_code(models: list[dict]) -> str:
    """모델 구조 → seed.ts 코드 생성."""
    has_user = any(m["name"] == "User" for m in models)
    has_password = has_user and any(
        f["name"] == "password" for m in models if m["name"] == "User" for f in m["fields"]
    )

    lines = [
        "import { PrismaClient } from '@prisma/client';",
    ]
    if has_password:
        lines.append("import * as bcrypt from 'bcrypt';")

    lines += [
        "",
        "const prisma = new PrismaClient();",
        "",
        "async function main() {",
        "  console.log('시드 데이터 생성 시작...');",
        "",
    ]

    if has_password:
        lines.append("  const pw = await bcrypt.hash('Test1234!', 12);")
        lines.append("")

    # User 모델이 있으면 테스트 계정 생성
    if has_user:
        user_model = next(m for m in models if m["name"] == "User")
        user_fields = {f["name"]: f for f in user_model["fields"]}

        for i, acc in enumerate(DEFAULT_ACCOUNTS):
            var_name = f"user{i + 1}"
            data_parts = []

            if "email" in user_fields:
                data_parts.append(f"      email: '{acc['email']}',")
            if "password" in user_fields:
                data_parts.append(f"      password: pw,")
            if "name" in user_fields:
                data_parts.append(f"      name: '{acc['name']}',")
            if "nickname" in user_fields:
                data_parts.append(f"      nickname: '{acc['nickname']}',")
            if "role" in user_fields:
                data_parts.append(f"      role: '{acc['role']}',")
            if "phone" in user_fields:
                data_parts.append(f"      phone: '010-0000-000{i}',")

            lines.append(f"  const {var_name} = await prisma.user.create({{")
            lines.append(f"    data: {{")
            lines.extend(data_parts)
            lines.append(f"    }},")
            lines.append(f"  }});")
            lines.append(f"  console.log('사용자 생성:', {var_name}.email);")
            lines.append("")

        # User와 관계가 있는 모델에 샘플 데이터 생성
        for model in models:
            if model["name"] == "User":
                continue

            user_fk = None
            required_fields = []
            for f in model["fields"]:
                if f["name"] == "userId" or (f["name"].endswith("Id") and not f["is_id"]):
                    if f["name"] == "userId":
                        user_fk = f["name"]
                if not f["is_id"] and not f["has_default"] and not f["optional"] and not f["is_relation"]:
                    required_fields.append(f)

            if not user_fk or not required_fields:
                continue

            # 샘플 데이터 1건 생성
            model_lower = model["name"][0].lower() + model["name"][1:]
            lines.append(f"  // {model['name']} 샘플")
            lines.append(f"  try {{")
            lines.append(f"    await prisma.{model_lower}.create({{")
            lines.append(f"      data: {{")
            lines.append(f"        userId: user1.id,")

            for f in required_fields:
                if f["name"] == "userId":
                    continue
                val = _sample_value(f)
                lines.append(f"        {f['name']}: {val},")

            lines.append(f"      }},")
            lines.append(f"    }});")
            lines.append(f"    console.log('{model['name']} 샘플 생성 완료');")
            lines.append(f"  }} catch (e: any) {{")
            lines.append(f"    console.log('{model['name']} 샘플 생성 스킵:', e.message?.slice(0, 80));")
            lines.append(f"  }}")
            lines.append("")

    lines += [
        "  console.log('시드 완료!');",
        "  console.log('테스트 계정: test@test.kr / Test1234!');",
        "  console.log('관리자 계정: admin@test.kr / Test1234!');",
        "}",
        "",
        "main()",
        "  .catch(console.error)",
        "  .finally(() => prisma.$disconnect());",
    ]

    return "\n".join(lines)


def _sample_value(field: dict) -> str:
    """필드 타입에 맞는 샘플 값 생성."""
    name = field["name"].lower()
    ftype = field["type"]

    if ftype == "String":
        if "email" in name:
            return "'sample@test.kr'"
        elif "phone" in name:
            return "'010-0000-0000'"
        elif "url" in name or "image" in name:
            return "'https://picsum.photos/200'"
        elif "address" in name:
            return "'서울시 강남구 테스트동 123'"
        elif "name" in name or "title" in name:
            return f"'샘플 {field['name']}'"
        elif "content" in name or "description" in name or "bio" in name:
            return "'자동 생성된 샘플 데이터입니다.'"
        elif "category" in name:
            return "'일반'"
        elif "code" in name:
            return "'SAMPLE001'"
        elif "status" in name:
            return "'ACTIVE'"
        elif "type" in name:
            return "'DEFAULT'"
        elif "species" in name:
            return "'DOG'"
        elif "gender" in name:
            return "'MALE'"
        elif "method" in name:
            return "'DEFAULT'"
        else:
            return f"'샘플_{field['name']}'"
    elif ftype == "Int":
        if "price" in name or "amount" in name:
            return "10000"
        elif "count" in name:
            return "0"
        elif "stock" in name:
            return "100"
        elif "rating" in name:
            return "5"
        elif "point" in name:
            return "1000"
        elif "quantity" in name:
            return "1"
        else:
            return "1"
    elif ftype == "Float":
        if "latitude" in name:
            return "37.5"
        elif "longitude" in name:
            return "127.0"
        elif "weight" in name:
            return "5.0"
        elif "rating" in name:
            return "4.5"
        else:
            return "1.0"
    elif ftype == "Boolean":
        return "true"
    elif ftype == "DateTime":
        return "new Date()"
    else:
        return f"'sample_{field['name']}'"


# ============================================================
# FastAPI (SQLAlchemy) seed 생성
# ============================================================

async def _generate_fastapi_seed(be: Path) -> bool:
    """FastAPI 프로젝트의 SQLAlchemy 모델 분석 → seed.py 생성 + 실행."""
    seed_path = be / "seed.py"
    if seed_path.is_file():
        logger.info("seed_already_exists path=%s", seed_path)
        return True

    # SQLAlchemy 모델에서 User 클래스 탐색
    user_model_file = None
    user_import_path = None
    has_password_field = False

    for py_file in be.rglob("*.py"):
        if ".venv" in str(py_file) or "seed" in py_file.name:
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("file_read_failed path=%s error=%s", py_file, exc)
            continue
        if re.search(r"class\s+User\b.*Base\b", content):
            user_model_file = py_file
            # import path 계산: app/models/user.py → app.models.user
            rel = py_file.relative_to(be).with_suffix("")
            user_import_path = str(rel).replace("/", ".").replace("\\", ".")
            has_password_field = "password" in content or "hashed_password" in content
            break

    if not user_model_file:
        logger.info("fastapi_seed_skipped reason=no_user_model")
        return False

    # DB 세션 임포트 경로 탐색
    db_import = "app.database"
    for py_file in be.rglob("*.py"):
        if ".venv" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("file_read_failed path=%s error=%s", py_file, exc)
            continue
        if "SessionLocal" in content or "get_db" in content:
            rel = py_file.relative_to(be).with_suffix("")
            db_import = str(rel).replace("/", ".").replace("\\", ".")
            break

    # 비밀번호 해시 방식 감지
    hash_import = ""
    hash_call = "\"Test1234!\""
    for py_file in be.rglob("*.py"):
        if ".venv" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("file_read_failed path=%s error=%s", py_file, exc)
            continue
        if "passlib" in content or "bcrypt" in content:
            if "CryptContext" in content:
                hash_import = "from passlib.context import CryptContext\npwd_ctx = CryptContext(schemes=['bcrypt'])"
                hash_call = "pwd_ctx.hash('Test1234!')"
            elif "hashlib" in content:
                hash_import = "import hashlib"
                hash_call = "hashlib.sha256('Test1234!'.encode()).hexdigest()"
            break

    # 비밀번호 필드명 감지
    pw_field = "password"
    user_content = user_model_file.read_text(encoding="utf-8")
    if "hashed_password" in user_content:
        pw_field = "hashed_password"
    elif "password_hash" in user_content:
        pw_field = "password_hash"

    seed_code = f'''"""Auto-generated seed data."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from {db_import} import SessionLocal
from {user_import_path} import User
{hash_import}

def main():
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == "test@test.kr").first()
        if existing:
            print("시드 데이터 이미 존재")
            return

        pw = {hash_call}
        users = [
            User(email="test@test.kr", {pw_field}=pw, name="테스트유저", role="USER"),
            User(email="admin@test.kr", {pw_field}=pw, name="관리자", role="ADMIN"),
        ]
        for u in users:
            try:
                db.add(u)
                db.commit()
                print(f"사용자 생성: {{u.email}}")
            except Exception as e:
                db.rollback()
                print(f"스킵: {{e}}")

        print("시드 완료! 테스트 계정: test@test.kr / Test1234!")
    finally:
        db.close()

if __name__ == "__main__":
    main()
'''
    seed_path.write_text(seed_code, encoding="utf-8")
    logger.info("fastapi_seed_generated path=%s", seed_path)

    # 실행
    result = await asyncio.to_thread(subprocess.run, 
        ["python3", "seed.py"],
        cwd=be, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        logger.info("fastapi_seed_ok output=%s", result.stdout.strip())
        return True
    else:
        logger.warning("fastapi_seed_failed stderr=%s", result.stderr[-300:])
        return False


async def _generate_django_seed(be: Path) -> bool:
    """Django 프로젝트의 테스트 계정 생성."""
    seed_path = be / "seed.py"
    if seed_path.is_file():
        logger.info("django_seed_exists path=%s", seed_path)
        return True

    # settings 모듈 찾기
    settings_module = "config.settings"
    for py_file in be.rglob("settings.py"):
        if ".venv" in str(py_file):
            continue
        rel = py_file.relative_to(be).with_suffix("")
        settings_module = str(rel).replace("/", ".").replace("\\", ".")
        break

    seed_code = f'''"""Auto-generated Django seed data."""
import os, sys, django
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{settings_module}")
django.setup()

from django.contrib.auth import get_user_model
User = get_user_model()

def main():
    for email, name, is_staff in [
        ("test@test.kr", "테스트유저", False),
        ("admin@test.kr", "관리자", True),
    ]:
        if not User.objects.filter(email=email).exists():
            if is_staff:
                User.objects.create_superuser(email=email, password="Test1234!", username=name)
            else:
                User.objects.create_user(email=email, password="Test1234!", username=name)
            print(f"사용자 생성: {{email}}")
        else:
            print(f"이미 존재: {{email}}")
    print("시드 완료! 테스트 계정: test@test.kr / Test1234!")

if __name__ == "__main__":
    main()
'''
    seed_path.write_text(seed_code, encoding="utf-8")

    result = await asyncio.to_thread(subprocess.run, 
        ["python3", "seed.py"], cwd=be, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        logger.info("django_seed_ok")
        return True
    else:
        logger.warning("django_seed_failed stderr=%s", result.stderr[-300:])
        return False


async def _generate_sql_seed(be: Path) -> bool:
    """Spring/Go 등 — SQL 기반 범용 시드 생성. DB 스키마에서 users 테이블 감지."""
    seed_path = be / "seed.sql"
    if seed_path.is_file():
        logger.info("sql_seed_exists path=%s", seed_path)
        return True

    # SQL 마이그레이션/스키마 파일에서 users 테이블 탐색
    has_users_table = False
    for sql_file in be.rglob("*.sql"):
        try:
            content = sql_file.read_text(encoding="utf-8")
        except Exception as exc:
            logger.debug("file_read_failed path=%s error=%s", sql_file, exc)
            continue
        if re.search(r"CREATE\s+TABLE\s+\w*users?\b", content, re.IGNORECASE):
            has_users_table = True
            break

    # Entity/Model 파일에서도 탐색
    if not has_users_table:
        for src_file in list(be.rglob("*.java")) + list(be.rglob("*.go")):
            try:
                content = src_file.read_text(encoding="utf-8")
            except Exception as exc:
                logger.debug("file_read_failed path=%s error=%s", src_file, exc)
                continue
            if re.search(r'(?:@Table|@Entity).*user|type\s+User\s+struct', content, re.IGNORECASE):
                has_users_table = True
                break

    if not has_users_table:
        logger.info("sql_seed_skipped reason=no_users_table")
        return False

    # bcrypt hash of "Test1234!"
    pw_hash = "$2b$12$LJ3m4ys3Hz4bIWBGSHqKi.UBcMVcYPHMXjlMOaIVfTMfsNPbBInie"

    seed_sql = f"""-- Auto-generated seed data
-- 테스트 계정: test@test.kr / Test1234!, admin@test.kr / Test1234!

INSERT INTO users (email, password, name, role, created_at)
SELECT 'test@test.kr', '{pw_hash}', '테스트유저', 'USER', CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'test@test.kr');

INSERT INTO users (email, password, name, role, created_at)
SELECT 'admin@test.kr', '{pw_hash}', '관리자', 'ADMIN', CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'admin@test.kr');
"""
    seed_path.write_text(seed_sql, encoding="utf-8")
    logger.info("sql_seed_generated path=%s", seed_path)
    # SQL 실행은 DB 종류에 따라 다르므로 파일만 생성. 서버 기동 후 자동 실행 안 함.
    return True
