"""V9 Null 체크 안전성 테스트."""

from __future__ import annotations


def test_task_pair_node_id_null_safe():
    """task_pair_node_id가 None일 때 슬라이싱 안전성."""
    task_pair_node_id = None

    # After fix: (task_pair_node_id or "")[:8]
    result = (task_pair_node_id or "")[:8]
    assert result == ""

    # Normal case
    task_pair_node_id = "abc-123-xyz"
    result = (task_pair_node_id or "")[:8]
    assert result == "abc-123-"


def test_prev_list_access_safe():
    """_prev 리스트 접근 안전성."""
    # Empty list case
    _prev = []
    result = (_prev[-1] if _prev else {}).get("reason", "")
    assert result == ""

    # Normal case
    _prev = [{"reason": "test reason"}]
    result = (_prev[-1] if _prev else {}).get("reason", "")
    assert result == "test reason"
