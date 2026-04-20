# 풀 반응형 (Full Responsive / Fluid) 의무 규칙 — 모든 HTML 산출물 공통

**이 규칙은 엔진 전역 정책입니다 (CLAUDE.md 연동).**
HTML/웹 산출물을 생성하는 모든 skill 은 아래를 반드시 따라야 합니다.

## 1. 절대 원칙

- **적응형 (Adaptive) 금지** — 특정 breakpoint 에서만 작동하는 고정 레이아웃 금지
- **풀 반응형 (Fluid) 의무** — 모바일 320~375px 부터 데스크톱 1280px+ 까지 연속적 대응
- 모든 뷰포트에서 **가로 스크롤 없음** + **카드/콘텐츠 잘림 없음**

## 2. `<style>` 최상단 safeguard CSS (MANDATORY)

```css
/* Responsive safeguard — 반드시 포함. 제거 시 산출물 무효 */
*, *::before, *::after { min-width: 0; box-sizing: border-box; }
html, body { overflow-wrap: anywhere; word-break: keep-all; }
img, video, iframe, svg { max-width: 100%; height: auto; }
table { max-width: 100%; display: block; overflow-x: auto; }
pre, code { white-space: pre-wrap; word-break: break-all; overflow-x: auto; }
h1, h2, h3, h4, h5, h6, p, a, span, button, label { overflow-wrap: anywhere; }
```

## 3. 레이아웃 필수 패턴

### 3-1. 그리드 (카드 목록 등)
```css
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
  gap: 16px;
}
```
→ 모바일: 1 열, 태블릿: 2~3 열, 데스크톱: 4 열 자동 리플로우

### 3-2. Flex 행
```css
.row { display: flex; flex-wrap: wrap; gap: 12px; }
.row > * { flex: 1 1 min(240px, 100%); min-width: 0; }
```
→ 좁으면 세로 쌓기, 넓으면 가로 정렬

### 3-3. 테이블 (좁은 뷰포트)
- 기본: `overflow-x: auto` 로 자체 스크롤
- 모바일 최적: `@media (max-width: 600px) { table, thead, tbody, tr, td { display: block; } }` 로 카드 리스트 전환

## 4. 금지 패턴

- ❌ 고정 `width: 900px` 같은 px 기반 컨테이너 폭
- ❌ `display: flex` + `flex: 0 0 300px` 같은 고정 basis (좁은 뷰포트에서 overflow)
- ❌ 특정 breakpoint 에서만 reflow (e.g. `@media (min-width: 1024px)` 에서만 grid 전환)
- ❌ 화면 시안 카탈로그를 가로로 나열 (각 화면은 세로 스택 + 개별 풀반응형)

## 5. 시안 카탈로그 (화면 모음) 특수 규칙

여러 화면을 한 HTML 에 모아 보여주는 경우 (UI 디자인 시안 등):
- 각 `<section>` (화면) 은 **독립 풀반응형 컴포넌트**
- 섹션 간 **세로 배치** (가로 grid 금지) — 모바일에서도 정상 폭 유지
- 각 섹션 최대폭 `max-width: 1280px; margin: 0 auto;`
- 섹션 헤더 (SC-XX-NNN 라벨) 는 `position: sticky` 권장
