"""
engine/workspace/config_gen.py
package.json / tsconfig / .env / next.config / swagger / design-token generation.

Extracted from auto_deploy.py for modularity.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from engine.workspace.paths import _npm_safe_name, _is_node_builtin_or_skip

logger = logging.getLogger("engine.workspace.config_gen")


def _generate_nextjs_package_json(fe: Path) -> None:
    """import문 분석 → 의존성 자동 추출 → package.json 생성."""
    deps: dict[str, str] = {
        "next": "^15.0.0",
        "react": "^19.0.0",
        "react-dom": "^19.0.0",
    }
    # 소스 파일에서 import 패키지 추출 (node_modules, .next 제외)
    src_dir = fe / "src"
    _scan_dirs = [src_dir] if src_dir.is_dir() else [fe]
    for scan_dir in _scan_dirs:
        for ext in ("*.ts", "*.tsx"):
            for f in scan_dir.rglob(ext):
                if "node_modules" in str(f) or ".next" in str(f):
                    continue
                try:
                    content = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                for m in re.finditer(r"from\s+['\"]([^./][^'\"]*)['\"]", content):
                    raw_import = m.group(1)
                    if raw_import.startswith("@/"):
                        continue
                    pkg = raw_import.split("/")[0]
                    if pkg.startswith("@") and "/" in raw_import:
                        pkg = raw_import.split("/")[0] + "/" + raw_import.split("/")[1]
                    if pkg not in deps and pkg not in ("react", "next") and not _is_node_builtin_or_skip(pkg):
                        deps[pkg] = "latest"

    # 알려진 패키지 버전 매핑
    known = {
        "zustand": "^5.0.0",
        "@tanstack/react-query": "^5.0.0",
        "axios": "^1.7.0",
        "clsx": "^2.1.0",
        "framer-motion": "^11.0.0",
    }
    for k, v in known.items():
        if k in deps:
            deps[k] = v

    # 잘못 들어간 Node 내장 모듈 제거
    for bad in list(deps):
        if _is_node_builtin_or_skip(bad):
            del deps[bad]

    pkg = {
        "name": _npm_safe_name(fe.parent.name) + "-frontend",
        "version": "1.0.0",
        "private": True,
        "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
        "dependencies": deps,
        "devDependencies": {
            "typescript": "^5.0.0",
            "@types/react": "^19.0.0",
            "@types/react-dom": "^19.0.0",
            "@types/node": "^22.0.0",
        },
    }
    (fe / "package.json").write_text(json.dumps(pkg, indent=2, ensure_ascii=False))


def _generate_nextjs_tsconfig(fe: Path) -> None:
    tsconfig = {
        "compilerOptions": {
            "target": "ES2017", "lib": ["dom", "dom.iterable", "esnext"],
            "allowJs": True, "skipLibCheck": True, "strict": True, "noEmit": True,
            "esModuleInterop": True, "module": "esnext", "moduleResolution": "bundler",
            "resolveJsonModule": True, "isolatedModules": True, "jsx": "preserve",
            "incremental": True, "paths": {"@/*": ["./src/*"]},
            "plugins": [{"name": "next"}],
        },
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
        "exclude": ["node_modules"],
    }
    (fe / "tsconfig.json").write_text(json.dumps(tsconfig, indent=2))


def _generate_express_package_json(be: Path) -> None:
    """import문 분석 → Express 백엔드 package.json 생성."""
    deps: dict[str, str] = {"express": "^4.21.0"}
    dev_deps: dict[str, str] = {"typescript": "^5.0.0", "@types/node": "^22.0.0"}

    known_deps = {
        "cors": "^2.8.5", "helmet": "^8.0.0", "compression": "^1.7.4",
        "dotenv": "^16.4.0", "jsonwebtoken": "^9.0.0", "bcrypt": "^5.1.0",
        "zod": "^3.23.0", "axios": "^1.7.0", "socket.io": "^4.8.0",
        "swagger-ui-express": "^5.0.0", "swagger-jsdoc": "^6.2.0",
        "express-rate-limit": "^7.4.0", "@prisma/client": "^6.0.0",
    }
    known_dev = {
        "@types/express": "^5.0.0", "@types/cors": "^2.8.0",
        "@types/compression": "^1.7.0", "@types/jsonwebtoken": "^9.0.0",
        "@types/bcrypt": "^5.0.0", "@types/swagger-ui-express": "^4.1.0",
        "@types/swagger-jsdoc": "^6.0.0", "prisma": "^6.0.0",
        "nodemon": "^3.1.0", "ts-node": "^10.9.0",
    }

    for ext in ("*.ts",):
        for f in be.rglob(ext):
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in re.finditer(r"from\s+['\"]([^./][^'\"]*)['\"]", content):
                raw_import = m.group(1)
                if raw_import.startswith("@/"):
                    continue
                pkg = raw_import.split("/")[0]
                if pkg.startswith("@") and "/" in raw_import:
                    pkg = "/".join(raw_import.split("/")[:2])
                if pkg in known_deps:
                    deps[pkg] = known_deps[pkg]
                elif pkg in known_dev:
                    dev_deps[pkg] = known_dev[pkg]
                elif pkg not in deps and not pkg.startswith("node:"):
                    types_pkg = f"@types/{pkg}"
                    if types_pkg in known_dev:
                        dev_deps[types_pkg] = known_dev[types_pkg]
                    deps[pkg] = "latest"

    pkg = {
        "name": _npm_safe_name(be.parent.name) + "-api",
        "version": "1.0.0",
        "private": True,
        "scripts": {
            "dev": "nodemon", "build": "tsc",
            "start": "node dist/server.js",
            "prisma:generate": "prisma generate",
            "prisma:push": "prisma db push",
        },
        "dependencies": deps,
        "devDependencies": dev_deps,
    }
    (be / "package.json").write_text(json.dumps(pkg, indent=2, ensure_ascii=False))


def _generate_express_tsconfig(be: Path) -> None:
    tsconfig = {
        "compilerOptions": {
            "target": "ES2022", "module": "commonjs", "lib": ["ES2022"],
            "outDir": "./dist", "rootDir": "./src",
            "strict": True, "esModuleInterop": True,
            "allowSyntheticDefaultImports": True, "skipLibCheck": True,
            "forceConsistentCasingInFileNames": True, "resolveJsonModule": True,
            "declaration": True, "sourceMap": True, "moduleResolution": "node",
        },
        "include": ["src/**/*.ts"],
        "exclude": ["node_modules", "dist"],
    }
    (be / "tsconfig.json").write_text(json.dumps(tsconfig, indent=2))


def _generate_dev_env(be: Path, be_port: int = 4000, fe_port: int = 3000) -> None:
    slug = _npm_safe_name(be.parent.name)
    env = f"""NODE_ENV=development
PORT={be_port}
DATABASE_URL=file:./prisma/dev.db
JWT_ACCESS_SECRET={slug}-dev-access-secret
JWT_REFRESH_SECRET={slug}-dev-refresh-secret
CORS_ORIGIN=http://localhost:{fe_port}
"""
    (be / ".env").write_text(env)


def _generate_frontend_env(fe: Path, be_port: int = 4000, api_prefix: str = "/api/v1") -> None:
    """프론트엔드 .env.local 생성 — 백엔드 API URL 자동 연결."""
    env = f"NEXT_PUBLIC_API_URL=http://localhost:{be_port}{api_prefix}\n"
    (fe / ".env.local").write_text(env)


def _apply_design_tokens(workspace_path: Path, project_id: str, db) -> bool:
    """DB에서 디자인 토큰 JSON을 로드 → 프론트엔드 globals.css에 CSS 변수 주입.

    기존 globals.css의 :root 블록을 디자인 토큰 기반으로 교체.
    AI가 잘못 생성한 컬러/폰트를 확정된 디자인 토큰으로 덮어쓴다.
    """
    fe_globals = workspace_path / "frontend" / "src" / "app" / "globals.css"
    if not fe_globals.is_file():
        return False

    # DB에서 디자인 토큰 로드 (동기 — asyncio.to_thread에서 호출됨)
    import sqlite3 as _sqlite3
    db_path = Path(__file__).resolve().parent.parent.parent / "platform.db"
    if not db_path.is_file():
        return False

    conn = _sqlite3.connect(str(db_path))
    conn.row_factory = _sqlite3.Row
    row = conn.execute(
        """SELECT av.storage_path FROM nodes n
           JOIN artifacts a ON a.node_id = n.id
           JOIN artifact_versions av ON av.artifact_id = a.id
           WHERE n.project_id = ? AND n.name = '디자인 토큰'
             AND n.node_type = 'TASK' AND n.state = 'COMPLETED'
           ORDER BY av.version_num DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    conn.close()

    if not row or not row["storage_path"]:
        return False

    try:
        tokens = json.loads(row["storage_path"])
    except (json.JSONDecodeError, TypeError):
        return False

    # CSS 변수 블록 생성
    css_lines = [":root {"]
    colors = tokens.get("colors", {})
    for key, val in colors.items():
        css_lines.append(f"  --{key.replace('_', '-')}: {val};")

    typo = tokens.get("typography", {})
    font = typo.get("font_family", "")
    if font:
        css_lines.append(f"  --font-display: '{font}', sans-serif;")
        css_lines.append(f"  --font-body: '{font}', sans-serif;")
    if typo.get("body_size"):
        css_lines.append(f"  --text-base: {typo['body_size']};")

    spacing = tokens.get("spacing", {})
    for key, val in spacing.items():
        css_lines.append(f"  --space-{key}: {val};")

    effects = tokens.get("effects", {})
    for key, val in effects.items():
        css_lines.append(f"  --{key.replace('_', '-')}: {val};")

    css_lines.append("}")

    # 기존 globals.css 읽기
    existing = fe_globals.read_text(encoding="utf-8")

    # :root { ... } 블록 교체
    new_root = "\n".join(css_lines)
    if ":root {" in existing:
        # 기존 :root 블록을 찾아서 교체
        import re as _re
        replaced = _re.sub(
            r':root\s*\{[^}]*\}',
            new_root,
            existing,
            count=1,
        )
        if replaced != existing:
            fe_globals.write_text(replaced, encoding="utf-8")
            logger.info("design_tokens_applied_to_globals path=%s vars=%d", fe_globals, len(css_lines) - 2)
            return True

    # :root가 없으면 파일 맨 앞에 삽입
    # @import 문이 있으면 그 뒤에 삽입
    import re as _re
    imports = _re.findall(r"@import[^;]+;", existing)
    if imports:
        last_import = imports[-1]
        insert_pos = existing.index(last_import) + len(last_import)
        new_content = existing[:insert_pos] + "\n\n" + new_root + "\n\n" + existing[insert_pos:]
    else:
        new_content = new_root + "\n\n" + existing

    fe_globals.write_text(new_content, encoding="utf-8")
    logger.info("design_tokens_inserted_to_globals path=%s vars=%d", fe_globals, len(css_lines) - 2)
    return True


def _generate_swagger(workspace_path: Path, stack: dict, be_port: int) -> int:
    """백엔드 라우터 파싱 → swagger-auto.json 생성. 엔드포인트 수 반환."""
    be = workspace_path / "backend"
    if not be.is_dir():
        return 0

    routes = []
    # Express/NestJS 라우트 파싱
    for ts_file in be.rglob("*.ts"):
        if "node_modules" in str(ts_file):
            continue
        try:
            code = ts_file.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = str(ts_file.relative_to(be))
        # router.get/post/put/patch/delete('/path', ...)
        for m in re.finditer(r'\.(get|post|put|patch|delete)\s*\(\s*[\'"`](\/[^\'"` ]*)[\'"`]', code, re.IGNORECASE):
            routes.append({"method": m.group(1).upper(), "path": m.group(2), "file": rel})
        # @Get/@Post 등 NestJS 데코레이터
        for m in re.finditer(r'@(Get|Post|Put|Patch|Delete)\s*\(\s*[\'"`]?(\/[^\'"` )]*)?[\'"`]?\s*\)', code, re.IGNORECASE):
            routes.append({"method": m.group(1).upper(), "path": m.group(2) or "/", "file": rel})

    # app.use prefix 추출 + 적용
    for app_file in ("src/app.ts", "src/index.ts", "app.ts", "server.ts"):
        fp = be / app_file
        if not fp.is_file():
            continue
        try:
            code = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r'app\.use\s*\(\s*[\'"`](\/[^\'"` ]*)[\'"`]\s*,\s*(\w+)', code):
            prefix, var_name = m.group(1), m.group(2)
            # require('./routes/xxx') 매핑
            for rm in re.finditer(rf'(?:const|let|var)\s+{re.escape(var_name)}\s*=\s*require\s*\(\s*[\'"`]\./(?:src/)?(?:routes?|routers?)/(\w+)', code):
                route_file_name = rm.group(1)
                for r in routes:
                    if route_file_name in r["file"] and not r.get("prefixed"):
                        r["path"] = prefix + (r["path"] if r["path"] != "/" else "")
                        r["prefixed"] = True

    if not routes:
        return 0

    # 중복 제거
    unique = []
    seen = set()
    for r in routes:
        key = f"{r['method']} {r['path']}"
        if key not in seen:
            seen.add(key)
            unique.append(r)

    # OpenAPI 3.0 스펙 생성
    paths = {}
    for r in unique:
        if r["path"] not in paths:
            paths[r["path"]] = {}
        method = r["method"].lower()
        entry = {
            "summary": f"{r['method']} {r['path']}",
            "tags": [r["file"].replace("src/", "").split("/")[0]],
            "responses": {"200": {"description": "OK"}, "401": {"description": "Unauthorized"}},
        }
        # path parameter 추출
        params = re.findall(r':(\w+)', r["path"])
        if params:
            entry["parameters"] = [
                {"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                for p in params
            ]
        paths[r["path"]] = {**paths[r["path"]], method: entry}

    openapi = {
        "openapi": "3.0.0",
        "info": {"title": f"{workspace_path.name} API", "version": "1.0.0",
                 "description": f"Auto-generated ({len(unique)} endpoints)"},
        "servers": [{"url": f"http://localhost:{be_port}"}],
        "paths": paths,
    }

    swagger_path = be / "swagger-auto.json"
    swagger_path.write_text(json.dumps(openapi, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("swagger_generated path=%s endpoints=%d", swagger_path, len(unique))
    return len(unique)
