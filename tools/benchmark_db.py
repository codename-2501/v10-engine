"""
tools/benchmark_db.py
pytest 없이 독립 실행 — 순수 SQLite 성능 측정.

실행: python tools/benchmark_db.py [db_path]
"""

from __future__ import annotations

import sqlite3
import sys
import time


def benchmark_concurrent_reads(db_path: str, iterations: int = 1000) -> None:
    conn = sqlite3.connect(db_path)
    # 테이블 없으면 스킵
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
    ).fetchall()
    if not tables:
        print("nodes 테이블 없음 — 읽기 벤치마크 스킵")
        conn.close()
        return

    start = time.perf_counter()
    for _ in range(iterations):
        conn.execute("SELECT COUNT(*) FROM nodes WHERE state='READY'").fetchone()
    elapsed = time.perf_counter() - start
    print(f"읽기 {iterations}회: {elapsed:.3f}초, {iterations/elapsed:.0f} QPS")
    conn.close()


def benchmark_write_transaction(db_path: str, iterations: int = 100) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # 임시 테이블
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _bench_nodes (id TEXT PRIMARY KEY, version INTEGER DEFAULT 0)"
    )
    for i in range(iterations):
        conn.execute(
            "INSERT OR IGNORE INTO _bench_nodes (id) VALUES (?)", (f"node_{i}",)
        )
    conn.commit()

    start = time.perf_counter()
    for i in range(iterations):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE _bench_nodes SET version=version+1 WHERE id=?", (f"node_{i}",)
        )
        conn.execute("COMMIT")
    elapsed = time.perf_counter() - start
    print(f"쓰기(BEGIN IMMEDIATE) {iterations}회: {elapsed:.3f}초, {iterations/elapsed:.0f} TPS")

    conn.execute("DROP TABLE _bench_nodes")
    conn.commit()
    conn.close()


def benchmark_wal_checkpoint(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    start = time.perf_counter()
    result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    elapsed = time.perf_counter() - start
    print(f"WAL checkpoint(TRUNCATE): {elapsed*1000:.1f}ms, result={result}")
    conn.close()


def benchmark_vacuum_into(db_path: str) -> None:
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    conn = sqlite3.connect(db_path)
    start = time.perf_counter()
    try:
        conn.execute(f"VACUUM INTO '{tmp}'")
        elapsed = time.perf_counter() - start
        size_mb = os.path.getsize(tmp) / 1024 / 1024
        print(f"VACUUM INTO: {elapsed:.3f}초, 크기={size_mb:.2f}MB")
    except Exception as exc:
        print(f"VACUUM INTO 실패: {exc}")
    finally:
        conn.close()
        os.unlink(tmp)


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "platform.db"
    print(f"\n=== SQLite 벤치마크: {db_path} ===\n")
    benchmark_concurrent_reads(db_path)
    benchmark_write_transaction(db_path)
    benchmark_wal_checkpoint(db_path)
    benchmark_vacuum_into(db_path)
    print("\n완료.")
