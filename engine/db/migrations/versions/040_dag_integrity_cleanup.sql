-- 040: V10 — 기존 DAG 누적 정합성 이슈 자동 정리
-- UP:

-- 누적 케이스 (운영 중 발견):
--   1. SKIPPED 노드의 활성 outgoing edge → 다운스트림 영구 미충족
--   2. 깨진 페어 link (qa_pair_node_id / task_pair_node_id 가 삭제된 노드 가리킴)
--   3. 고아 edge (from/to 노드 자체가 없음)
--
-- Migration 후엔 verify_dag_integrity 도구가 startup hook 으로 정기 검증.

-- 1. SKIPPED 노드의 활성 outgoing edge 비활성화
UPDATE edges
SET is_active = 0
WHERE is_active = 1
  AND from_node_id IN (SELECT id FROM nodes WHERE state = 'SKIPPED');

-- 2. 깨진 페어 link 정리 (NULL 처리 — 이후 dag_repair 로 이름 매칭 복원 가능)
UPDATE nodes
SET qa_pair_node_id = NULL
WHERE qa_pair_node_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM nodes q WHERE q.id = qa_pair_node_id);

UPDATE nodes
SET task_pair_node_id = NULL
WHERE task_pair_node_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM nodes t WHERE t.id = task_pair_node_id);

-- 3. 고아 edge 비활성화
UPDATE edges
SET is_active = 0
WHERE is_active = 1
  AND (NOT EXISTS (SELECT 1 FROM nodes WHERE id = edges.from_node_id)
       OR NOT EXISTS (SELECT 1 FROM nodes WHERE id = edges.to_node_id));

-- DOWN:
-- DOWN 불가 — 비활성화된 edge 복구 정보 손실. 별도 backup 필요 시 사전 dump.
