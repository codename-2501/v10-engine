"""
v8 AI SI Platform — BUILD 산출물에서 실제 프로젝트 추출 + 구동
코드 블록을 파싱해서 파일 구조로 생성하고 실행 가능한 프로젝트를 만듭니다.
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# 프로젝트 출력 디렉토리
OUTPUT_BASE = Path("/Users/codename/Downloads/v8/workspaces")


def get_build_artifacts(db_path="platform.db"):
    """BUILD 단계 산출물에서 코드 추출."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    artifacts = {}
    for name in ["프론트엔드 컴포넌트 구현", "백엔드 API 구현", "DB 스키마 및 마이그레이션 구현"]:
        ver = conn.execute("""
            SELECT av.storage_path AS content FROM artifact_versions av
            JOIN artifacts a ON a.id = av.artifact_id
            JOIN nodes n ON n.id = a.node_id
            WHERE n.name=? AND n.node_type='TASK'
            ORDER BY av.version_num DESC LIMIT 1
        """, (name,)).fetchone()
        if ver:
            artifacts[name] = ver["content"]

    # 프로젝트 이름
    eng = conn.execute("SELECT name FROM engagements LIMIT 1").fetchone()
    project_name = eng["name"] if eng else "project"

    conn.close()
    return artifacts, project_name


def parse_code_blocks(content: str) -> list[dict]:
    """마크다운에서 코드 블록 + 파일명/컴포넌트명 추출."""
    results = []

    # 패턴: 헤딩이나 볼드 텍스트 다음에 코드 블록
    # "### 1. Button" 또는 "**Button**" 다음에 ```tsx
    blocks = re.findall(
        r'(?:#{1,4}\s*(?:\d+\.?\s*)?)?(?:\*\*)?([^\n*`]+?)(?:\*\*)?\s*\n+```(\w+)\n([\s\S]*?)```',
        content
    )

    for heading, lang, code in blocks:
        heading = heading.strip().rstrip(":")
        # 파일명 추출 시도
        # "디자인 토큰 (globals.css)" → globals.css
        file_match = re.search(r'\(([^)]+\.\w+)\)', heading)
        if file_match:
            filename = file_match.group(1)
        else:
            # 컴포넌트명 → 파일명 변환
            name = heading.split("(")[0].strip()
            name = name.split("—")[0].strip()
            name = name.split("-")[0].strip()

            if lang == "tsx" or lang == "jsx":
                filename = f"{name}.tsx"
            elif lang == "ts":
                filename = f"{name}.ts"
            elif lang == "python" or lang == "py":
                filename = f"{name.lower().replace(' ', '_')}.py"
            elif lang == "sql":
                filename = f"{name.lower().replace(' ', '_')}.sql"
            elif lang == "css":
                filename = f"{name.lower().replace(' ', '_')}.css"
            else:
                filename = f"{name}.{lang}"

        # 파일명 안전 처리 (/, 공백, 특수문자)
        filename = filename.replace("/", "_").replace("\\", "_")
        filename = filename.replace(" ", "_").replace("·", "_")
        filename = re.sub(r'[^\w.\-]', '_', filename)
        filename = re.sub(r'_+', '_', filename).strip("_")

        results.append({
            "heading": heading,
            "lang": lang,
            "code": code.strip(),
            "filename": filename,
        })

    return results


def create_frontend_project(output_dir: Path, blocks: list[dict], project_name: str):
    """React/Next.js 프로젝트 생성."""
    src = output_dir / "frontend"
    src.mkdir(parents=True, exist_ok=True)

    # 파일 분류
    components_dir = src / "src" / "components"
    pages_dir = src / "src" / "app"
    styles_dir = src / "src" / "styles"
    lib_dir = src / "src" / "lib"

    for d in [components_dir, pages_dir, styles_dir, lib_dir]:
        d.mkdir(parents=True, exist_ok=True)

    for block in blocks:
        lang = block["lang"]
        filename = block["filename"]
        code = block["code"]
        heading = block["heading"].lower()

        if lang == "css":
            (styles_dir / filename).write_text(code, encoding="utf-8")
        elif "page" in heading or "layout" in heading or "app" in heading:
            (pages_dir / filename).write_text(code, encoding="utf-8")
        elif "api" in heading or "fetch" in heading or "client" in heading or "util" in heading:
            (lib_dir / filename).write_text(code, encoding="utf-8")
        else:
            (components_dir / filename).write_text(code, encoding="utf-8")

    # package.json
    pkg = {
        "name": project_name.lower().replace(" ", "-"),
        "version": "1.0.0",
        "private": True,
        "scripts": {
            "dev": "next dev",
            "build": "next build",
            "start": "next start",
        },
        "dependencies": {
            "next": "^15.0.0",
            "react": "^19.0.0",
            "react-dom": "^19.0.0",
        },
        "devDependencies": {
            "typescript": "^5.0.0",
            "@types/react": "^19.0.0",
            "@types/node": "^22.0.0",
        },
    }
    (src / "package.json").write_text(json.dumps(pkg, indent=2, ensure_ascii=False))

    # tsconfig.json
    tsconfig = {
        "compilerOptions": {
            "target": "ES2017",
            "lib": ["dom", "dom.iterable", "esnext"],
            "allowJs": True,
            "skipLibCheck": True,
            "strict": True,
            "noEmit": True,
            "esModuleInterop": True,
            "module": "esnext",
            "moduleResolution": "bundler",
            "resolveJsonModule": True,
            "isolatedModules": True,
            "jsx": "preserve",
            "incremental": True,
            "paths": {"@/*": ["./src/*"]},
        },
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx"],
        "exclude": ["node_modules"],
    }
    (src / "tsconfig.json").write_text(json.dumps(tsconfig, indent=2))

    # next.config.js
    (src / "next.config.js").write_text("/** @type {import('next').NextConfig} */\nmodule.exports = {}\n")

    print(f"  프론트엔드: {src} ({len(blocks)}개 파일)")
    return src


def create_backend_project(output_dir: Path, blocks: list[dict], project_name: str):
    """Python FastAPI 백엔드 프로젝트 생성."""
    src = output_dir / "backend"
    src.mkdir(parents=True, exist_ok=True)

    app_dir = src / "app"
    routes_dir = app_dir / "routes"
    models_dir = app_dir / "models"
    services_dir = app_dir / "services"

    for d in [app_dir, routes_dir, models_dir, services_dir]:
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")

    for block in blocks:
        filename = block["filename"]
        code = block["code"]
        heading = block["heading"].lower()

        if "model" in heading or "schema" in heading or "entity" in heading:
            (models_dir / filename).write_text(code, encoding="utf-8")
        elif "route" in heading or "endpoint" in heading or "api" in heading:
            (routes_dir / filename).write_text(code, encoding="utf-8")
        elif "service" in heading or "logic" in heading:
            (services_dir / filename).write_text(code, encoding="utf-8")
        elif "main" in heading or "app" in heading or "server" in heading:
            (app_dir / filename).write_text(code, encoding="utf-8")
        else:
            (app_dir / filename).write_text(code, encoding="utf-8")

    # requirements.txt
    reqs = "fastapi>=0.115.0\nuvicorn[standard]>=0.32.0\nsqlalchemy>=2.0.0\nalembic>=1.14.0\npydantic>=2.0.0\npython-jose[cryptography]>=3.3.0\npasslib[bcrypt]>=1.7.0\npython-multipart>=0.0.9\naiohttp>=3.10.0\n"
    (src / "requirements.txt").write_text(reqs)

    # run.sh
    (src / "run.sh").write_text("#!/bin/bash\npip install -r requirements.txt\nuvicorn app.main:app --host 0.0.0.0 --port 4000 --reload\n")
    os.chmod(src / "run.sh", 0o755)

    print(f"  백엔드: {src} ({len(blocks)}개 파일)")
    return src


def create_database_project(output_dir: Path, blocks: list[dict]):
    """DB 마이그레이션 파일 생성."""
    src = output_dir / "database"
    migrations_dir = src / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)

    for i, block in enumerate(blocks, 1):
        filename = f"{i:03d}_{block['filename']}"
        (migrations_dir / filename).write_text(block["code"], encoding="utf-8")

    # 시드 데이터 + README
    (src / "README.md").write_text(f"# Database\n\n{len(blocks)}개 마이그레이션 파일.\n\n```\npsql -f migrations/001_*.sql\n```\n")

    print(f"  데이터베이스: {src} ({len(blocks)}개 마이그레이션)")
    return src


def main():
    print("=" * 60)
    print("  v8 프로젝트 추출기")
    print("=" * 60)

    artifacts, project_name = get_build_artifacts()
    slug = project_name.lower().replace(" ", "-").replace("/", "-")
    output_dir = OUTPUT_BASE / slug

    if output_dir.exists():
        import shutil
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n프로젝트: {project_name}")
    print(f"출력: {output_dir}\n")

    # 프론트엔드
    if "프론트엔드 컴포넌트 구현" in artifacts:
        fe_blocks = parse_code_blocks(artifacts["프론트엔드 컴포넌트 구현"])
        fe_dir = create_frontend_project(output_dir, fe_blocks, project_name)

    # 백엔드
    if "백엔드 API 구현" in artifacts:
        be_blocks = parse_code_blocks(artifacts["백엔드 API 구현"])
        be_dir = create_backend_project(output_dir, be_blocks, project_name)

    # DB
    if "DB 스키마 및 마이그레이션 구현" in artifacts:
        db_blocks = parse_code_blocks(artifacts["DB 스키마 및 마이그레이션 구현"])
        db_dir = create_database_project(output_dir, db_blocks)

    # docker-compose.yml
    compose = f"""version: '3.8'
services:
  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    depends_on:
      - backend

  backend:
    build: ./backend
    ports:
      - "4000:4000"
    environment:
      - DATABASE_URL=${{DATABASE_URL}}
    depends_on:
      - db

  db:
    image: postgres:16
    ports:
      - "5432:5432"
    environment:
      - POSTGRES_DB={slug.replace('-', '_')}
      - POSTGRES_USER=admin
      - POSTGRES_PASSWORD=admin1234
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./database/migrations:/docker-entrypoint-initdb.d

volumes:
  pgdata:
"""
    (output_dir / "docker-compose.yml").write_text(compose)

    # README
    readme = f"""# {project_name}

## 구조
```
{slug}/
  frontend/     ← Next.js (React)
  backend/      ← FastAPI (Python)
  database/     ← PostgreSQL 마이그레이션
  docker-compose.yml
```

## 실행 방법

### 1. Docker (권장)
```bash
cd {output_dir}
docker-compose up -d
```
- 프론트엔드: http://localhost:3000
- 백엔드 API: http://localhost:4000
- DB: localhost:5432

### 2. 개별 실행
```bash
# 프론트엔드
cd frontend && npm install && npm run dev

# 백엔드
cd backend && pip install -r requirements.txt && uvicorn app.main:app --port 4000

# DB
psql -f database/migrations/*.sql
```
"""
    (output_dir / "README.md").write_text(readme)

    print(f"\n{'=' * 60}")
    print(f"  완료! 프로젝트: {output_dir}")
    print(f"{'=' * 60}")
    print(f"\n실행: cd {output_dir} && docker-compose up -d")
    print(f"프론트: http://localhost:3000")
    print(f"API:    http://localhost:4000")


if __name__ == "__main__":
    main()
