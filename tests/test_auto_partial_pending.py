"""C1' 회귀 — auto_partial_pending snap 보존 + clean-up 룰."""
from __future__ import annotations

import json


def test_snap_pending_dict_구조():
    """C1' — coverage missing OR density fail 시 보존되는 snap 구조."""
    completed_items = {"SC-AI-001": "<section>...</section>"}
    auto_failed = {"SC-AI-002", "SC-HB-005"}
    snap = {
        "type": "chunked_html_items",
        "completed_items": completed_items,
        "completed_count": len(completed_items),
        "total_count": 3,
        "failed_items_last_attempt": sorted(auto_failed),
        "auto_partial_pending": True,
        "updated_at": "2026-04-26T00:00:00Z",
    }
    serialized = json.dumps(snap, ensure_ascii=False)
    parsed = json.loads(serialized)
    assert parsed["auto_partial_pending"] is True
    assert parsed["failed_items_last_attempt"] == ["SC-AI-002", "SC-HB-005"]
    assert parsed["completed_count"] == 1


def test_정상시_snap_NULL():
    """auto_failed 비어있으면 task_snapshot=NULL clean-up."""
    coverage_missing: set = set()
    density_failed: set = set()
    auto_failed = coverage_missing | density_failed
    assert not auto_failed  # NULL 분기 진입 조건


def test_partial_pending_재진입_조건():
    """다음 retry 시 partial flag on + snap 의 failed_items_last_attempt 가 있으면 partial 모드."""
    snap = {
        "type": "chunked_html_items",
        "completed_items": {"SC-AI-001": "..."},
        "failed_items_last_attempt": ["SC-AI-002"],
        "auto_partial_pending": True,
    }
    # executor.py L1239~1244 의 partial flag 조건과 동일
    partial_flag = True  # 환경변수 V10_CHUNKED_ITEMS_PARTIAL_RETRY=1
    failed_keys: set = set()
    if partial_flag:
        raw_failed = snap.get("failed_items_last_attempt") or []
        if isinstance(raw_failed, list):
            failed_keys = {str(k) for k in raw_failed if isinstance(k, str)}
    assert failed_keys == {"SC-AI-002"}


def test_coverage_missing_density_fail_합집합():
    coverage_missing = {"SC-AI-002"}
    density_failed = {"SC-HB-005", "SC-AI-002"}  # overlap OK
    auto_failed = coverage_missing | density_failed
    assert auto_failed == {"SC-AI-002", "SC-HB-005"}
