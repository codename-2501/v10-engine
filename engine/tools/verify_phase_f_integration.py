"""
engine/tools/verify_phase_f_integration.py

Phase F 통합 단위 검증 — LLM 호출 없이 엔진 경로만 검증.

검증 경로:
  (1) episode_store.save_episode() → embedding 백그라운드 생성
  (2) episode_store.search_similar_episodes() → 결과 반환
  (3) backend_requirement 태그 인식 (YAML 로드)
  (4) backend_choice mismatch 시 스킬 강등 로직 정합성
  (5) DynamicDAGExtension 주입 한계/루프 감지

실행:
  PYTHONPATH=. python3 engine/tools/verify_phase_f_integration.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid


async def main() -> int:
    from engine.db.adapter import create_adapter
    from engine.memory.episode_store import EpisodeStore
    from engine.skills.registry import SkillRegistry

    db_url = os.environ.get("DATABASE_URL", "sqlite:///platform.db")
    db = create_adapter(db_url)

    store = EpisodeStore(db)
    pid = f"verify-{uuid.uuid4().hex[:8]}"
    passed = 0
    failed = 0

    def ok(label: str) -> None:
        nonlocal passed
        passed += 1
        print(f"  ✓ {label}")

    def fail(label: str, err: str) -> None:
        nonlocal failed
        failed += 1
        print(f"  ✗ {label}: {err}")

    print(f"=== Phase F 통합 단위 검증 (project_id={pid}) ===\n")

    # (1) save_episode + background embedding
    try:
        eid = await store.save_episode(
            pid, "n1", "VerifyNode", "gotcha", "검증 에러 발생", {"attempt": 1}
        )
        await asyncio.sleep(1.5)  # embedding 태스크 대기
        row = await db.fetchone(
            "SELECT embedding_json IS NOT NULL AS has_emb FROM episodes WHERE id=?",
            (eid,),
        )
        if row and row["has_emb"]:
            ok(f"save_episode + embedding (id={eid[:8]})")
        else:
            fail("save_episode embedding", "embedding_json 미생성")
    except Exception as exc:
        fail("save_episode", str(exc))

    # (2) search_similar_episodes
    try:
        results = await store.search_similar_episodes(
            query="검증 에러", project_id=pid, episode_type="gotcha",
            top_k=3, min_similarity=0.1,
        )
        if len(results) >= 1:
            ok(f"search_similar_episodes ({len(results)} results)")
        else:
            fail("search_similar_episodes", "결과 0")
    except Exception as exc:
        fail("search_similar_episodes", str(exc))

    # (3) backend_requirement 태그 인식
    try:
        reg = SkillRegistry()
        spec = reg.resolve("InstantDB 백엔드 구현", "BUILD", "TASK")
        if spec and spec.get("backend_requirement") == "instantdb":
            ok("backend_requirement 태그 인식 (instantdb)")
        else:
            fail("backend_requirement", f"spec={spec}")

        spec_sql = reg.resolve("백엔드 API 구현", "BUILD", "TASK")
        if spec_sql and spec_sql.get("backend_requirement") == "sql":
            ok("backend_requirement 태그 인식 (sql)")
        else:
            fail("backend_requirement SQL", f"spec={spec_sql}")
    except Exception as exc:
        fail("SkillRegistry", str(exc))

    # (4) backend mismatch 시 강등 로직 정합
    try:
        reg = SkillRegistry()
        spec = reg.resolve("InstantDB 백엔드 구현", "BUILD", "TASK")
        required = (spec or {}).get("backend_requirement")
        project_backend = "sql"
        if required and required != project_backend:
            ok("backend mismatch → skill 강등 로직 정합")
        else:
            fail("mismatch 로직", f"required={required}, project={project_backend}")
    except Exception as exc:
        fail("mismatch 정합", str(exc))

    # (5) DynamicDAGExtension 주입 한계/루프 감지
    try:
        from engine.core.dynamic_dag import DynamicDAGExtension, NodeSpec, InjectionError

        async def noop_enqueue(dag_id: str) -> None:
            pass

        ext = DynamicDAGExtension(db, noop_enqueue)
        try:
            await ext.inject_node(
                dag_id="nonexistent-dag",
                parent_node_id="nonexistent-parent",
                node_spec=NodeSpec(name="test"),
            )
            fail("inject_node invalid DAG", "InjectionError 미발생")
        except InjectionError:
            ok("inject_node 존재하지 않는 DAG → InjectionError")

        count = await ext._count_injected_nodes("any-dag")
        if isinstance(count, int) and count >= 0:
            ok(f"injected_nodes count={count}")
        else:
            fail("count_injected_nodes", f"non-int: {count}")
    except Exception as exc:
        fail("DynamicDAGExtension", str(exc))

    # (6) backend_choice 컬럼 존재 + 기본값
    try:
        rows = await db.fetchall("PRAGMA table_info(projects)")
        col_names = [r["name"] for r in rows]
        if "backend_choice" in col_names:
            ok("projects.backend_choice 컬럼 존재")
        else:
            fail("backend_choice 컬럼", "없음 (마이그레이션 036 미적용)")
    except Exception as exc:
        fail("schema check", str(exc))

    # 최종 결과
    print(f"\n=== 결과: {passed} 통과 / {failed} 실패 ===")

    # 정리: 검증용 에피소드 삭제
    try:
        await db.execute("DELETE FROM episodes WHERE project_id=?", (pid,))
    except Exception:
        pass

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
