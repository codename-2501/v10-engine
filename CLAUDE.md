# 작업 규칙

## 절대 규칙
- 확인/승인/컨펌 질문 금지 ("할까요?", "진행할까요?", "괜찮을까요?" 등)
- 즉시 처리하고 결과만 보고
- 모든 UI 액션은 즉시 반영 (새로고침 필요 없이)
- 코드 수정 후 코어 영향 분석 + 사이드이펙트 검토 필수 보고

## 핵심 코어 (절대 변경 금지)
- engine/core/dag_advancer.py — DAG 실행 엔진
- engine/core/state_machine.py — 상태 전이 테이블
- engine/ai/context_assembler.py — 5-Layer 컨텍스트 조립
- engine/core/budget_enforcer.py — 토큰 예산 관리
- engine/core/cascade.py — 변경 전파

## 구축 규칙
- 웹 서비스 구축은 무조건 반응형 웹 (모바일/태블릿/데스크톱)
- 네이티브 앱 요청이 아닌 이상 반응형 웹으로 제작

## 산출물 디자인 규칙
- AI스럽고 정형화된 디자인 금지
- 프로젝트 맥락에 맞는 독창적 디자인 (실버케어→따뜻한 톤, SaaS→모던 미니멀 등)
- 보라색 그라디언트, 쿠키커터 레이아웃, 기본 시스템 폰트 금지
- HTML 산출물은 실제 서비스 수준의 시각적 완성도 필수

## 프로젝트 구조
- 5단계: DEFINE → DESIGN → BUILD → VERIFY → DELIVER
- BUILD→VERIFY GATE는 자동 승인
- CLI 프록시: Max (enjoyfigma) 우선, Pro (zeroaibot2501) 보조
- 산출물 보기: 새 탭 HTML 렌더링 (마크다운은 서버에서 변환)
