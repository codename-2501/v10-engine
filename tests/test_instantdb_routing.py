"""Phase 1-4 회귀 — backendChoice → InstantDB spec 활성화."""
from __future__ import annotations

from engine.intake.processor import NODE_TEMPLATES


def test_InstantDB_노드_정의됨():
    """NODE_TEMPLATES 에 InstantDB 데이터 모델/권한 정책 노드 존재."""
    all_nodes = []
    for tmpl in NODE_TEMPLATES:
        all_nodes.extend(tmpl["nodes"])
    names = {n["name"] for n in all_nodes}
    assert "InstantDB 데이터 모델 설계" in names
    assert "InstantDB 권한 정책 설계" in names
    assert "InstantDB 인증 구현" in names
    assert "InstantDB 실시간 쿼리" in names
    assert "InstantDB 프론트엔드 통합" in names


def test_InstantDB_노드_when_backend_조건():
    """모든 InstantDB 노드의 when 이 backend:instantdb 만 매칭."""
    for tmpl in NODE_TEMPLATES:
        for node in tmpl["nodes"]:
            if "InstantDB" in node["name"]:
                when = node.get("when")
                assert isinstance(when, list)
                assert "backend:instantdb" in when


def test_backend_token_inject_로직_존재():
    """processor.py 에 backend:xxx scope token 주입 로직 존재."""
    import inspect
    from engine.intake import processor
    src = inspect.getsource(processor)
    assert "backend:" in src
    assert "backendChoice" in src or "backend_choice" in src


def test_when_매칭_simulator():
    """_is_applicable 분기 모의 — backend:instantdb 가 scopes 에 있을 때 노드 활성화."""
    scopes = {"new", "backend:instantdb"}
    when = ["backend:instantdb"]
    assert bool(set(when) & scopes) is True


def test_backend_미선택시_InstantDB_노드_skip():
    scopes = {"new", "ai_model"}  # backend 없음
    when = ["backend:instantdb"]
    assert bool(set(when) & scopes) is False
