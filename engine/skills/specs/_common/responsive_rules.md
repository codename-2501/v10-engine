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
- ❌ `<style>` 블록 내 `min-width: Xpx` (X > 320) — 모바일 viewport (375px) 초과 overflow 유발. 대신 `min-width: min(Xpx, 100%)` 사용 (saver 가 자동 치환).
- ❌ `button { white-space: nowrap }` + `word-break` 미선언 조합 — 긴 텍스트 잘림. wrap 허용 or 명시적 ellipsis 필요.

## 5. 시안 카탈로그 (화면 모음) 특수 규칙

여러 화면을 한 HTML 에 모아 보여주는 경우 (UI 디자인 시안 등):
- 각 `<section>` (화면) 은 **독립 풀반응형 컴포넌트**
- 섹션 간 **세로 배치** (가로 grid 금지) — 모바일에서도 정상 폭 유지
- 각 섹션 최대폭 `max-width: 1280px; margin: 0 auto;`
- **섹션 헤더 라벨 (SC-XX-NNN 형식) position 규칙**:
  - ❌ 절대 `position: absolute` 금지 — 내부 콘텐츠와 겹침
  - ✅ 권장: `position: static` 또는 `position: sticky; top: 0;` (스크롤 시 상단 고정 + 반투명 배경)
  - 콘텐츠 시작 전 **자연스러운 블록 요소** 로 배치 (flex/float 회피)

### 올바른 섹션 라벨 예시
```html
<section id="SC-AU-005" class="screen">
  <header class="screen-label" style="position: sticky; top: 0; z-index: 10;
          background: rgba(22,28,24,0.94); backdrop-filter: blur(8px);
          padding: 10px 16px; margin: 0 0 16px;
          font-size: 11px; letter-spacing: 0.08em;">
    SC-AU-005 | 비밀번호 재설정
  </header>
  <!-- 화면 실제 콘텐츠 -->
</section>
```

### 금지 예시 (겹침 유발)
```html
<!-- ❌ 이 방식은 내부 콘텐츠 위에 라벨이 덮어쓰기 -->
<header class="screen-label" style="position: absolute; top: 12px; left: 16px;">
  SC-XX-NNN | ...
</header>
```

## 6. HTML 태그 정합성 (필수)

- 모든 `<section>`, `<main>`, `<article>`, `<aside>`, `<nav>` 는 **반드시 close 태그 작성**
- 닫지 않으면 브라우저가 다음 섹션을 이전 섹션의 자식으로 파싱 → DOM 중첩으로 width 폭발적 축소 (부모 grid/flex shrink)
- 각 `<section id="SC-...">` 블록이 시작되기 전 **이전 section 이 반드시 닫혀있어야 함**

## 7. 디자인 시스템 사전 선언 (create system up front)

HTML `<body>` 시작 직전 또는 `<style>` 블록 상단에 **사용할 디자인 시스템을 JSON 주석으로 명시**. LLM 의 즉흥적 inline hex 남발을 방지하고 섹션 간 일관성 강제.

```html
<!-- DESIGN_SYSTEM_USED {
  "palette": {"--bg": "#xxx", "--surface": "#xxx", "--accent": "#xxx", "--text": "#xxx"},
  "typography": {"body": "15px/1.5", "h1": "40px/1.1", "h2": "28px/1.2", "h3": "20px/1.3"},
  "spacing_scale": [4, 8, 12, 16, 24, 32, 48, 64],
  "radius_scale": [8, 12, 16, 20],
  "section_header_pattern": "sticky top 0 + backdrop blur + uppercase label + subtitle",
  "background_variations": ["base", "elevated surface"]
} -->
```

- 이후 생성하는 모든 섹션/카드/버튼은 위 시스템에서만 값을 취한다.
- 선언하지 않은 색/타이포/spacing 사용 금지 → harness FAIL.

## 8. AI slop 금지 (디자인 품질 필수 요건)

다음 패턴은 **AI 가 생성한 티** 를 내는 고정관념 — 모두 금지:

- **과도한 그라디언트 배경** (특히 보라색/핑크색 조합, radial 반복 패턴)
- **브랜드 일부가 아닌 emoji 아이콘** — 랜덤 이모지 (🔥 ⚡ ✨ 💡 🎯 🚀 📊 등) 를 기능 아이콘 대신 사용 금지. 플랫폼/폰트별 렌더 편차 + 스크린 리더 "불, 번개" 로 읽음. 대신 `<span class="icon" aria-hidden="true"><svg>...</svg></span>` 또는 명시적 텍스트 배지.
- **유니코드 기호를 아이콘으로 사용** — `▲` `▼` `◆` `★` `●` `◎` `◇` `!` `‹` `›` `→` `←` 등 geometric/punctuation 문자를 기능 아이콘 대체용으로 사용 금지. 스크린 리더 혼란 + 모호한 의미. **아이콘이 필요하면 아래 §8-2 Lucide SVG 를 복사해서 사용** (Lucide ISC 라이선스, 상업 이용 무제한).

### 8-2. 권장 SVG 아이콘 (이모지/유니코드 대체 — 복사해서 사용)

**사용 원칙:**
- `width`/`height` 는 용도별 조정 (배지 14~16px, 본문 16~18px, 큰 강조 24px+)
- `stroke="currentColor"` 로 텍스트 색상 상속 → 테마/다크모드 자동 대응
- 장식용은 `aria-hidden="true"`, 의미 전달은 `<title>텍스트</title>` + `role="img"`
- Lucide 원본: https://lucide.dev (ISC 라이선스)

**flame (스트릭 / 🔥 대체):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>
```

**zap (에너지 / ⚡ 대체):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
```

**check (완료 / ✓ 표준화):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
```

**trending-up (증가 / ▲ 대체):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>
```

**trending-down (감소 / ▼ 대체):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="22 17 13.5 8.5 8.5 13.5 2 7"/><polyline points="16 17 22 17 22 11"/></svg>
```

**arrow-right (→ 대체):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
```

**chevron-left (‹ 대체, pagination):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-label="이전 페이지"><polyline points="15 18 9 12 15 6"/></svg>
```

**chevron-right (› 대체, pagination):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-label="다음 페이지"><polyline points="9 18 15 12 9 6"/></svg>
```

**star (강조 / ★ 대체):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
```

**alert-circle (경고 / ! 대체):**
```html
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
```

**필요한 다른 아이콘** 은 https://lucide.dev 에서 검색 → SVG source 복사. 같은 시각 언어 유지.
- **쿠키커터 카드 스타일** — 둥근 모서리 + 좌측 border accent color + 흐릿한 그림자 반복
- **SVG 로 실제 이미지/일러스트 drawing** — 만화 아이콘, 인물, 복잡한 그림 그리기 금지. `<div style="background:#22262f;aspect-ratio:1">사진 placeholder</div>` 식으로 명시적 플레이스홀더.
- **과용 폰트 금지: `Inter`, `Roboto`, `Arial`, `Fraunces`, 시스템 기본 (`-apple-system`)** — 프로젝트 브랜드에 맞는 의도 있는 타이포 선택
- **filler content / data slop** — 의미 없는 stats 숫자·icon 목록·lorem ipsum 식 더미. 섹션이 비어 보이면 레이아웃으로 해결, 내용으로 채우지 말 것.

## 9. 최소 수치 기준 (접근성 + 사용성)

- **터치 타겟** — `<button>`, `<a>`, 탭 가능 요소: **최소 44×44px** (iOS HIG 기준)
  ```css
  .btn, button, a.btn, [role="button"] {
    min-height: 44px;
    min-width: 44px;
    padding: 10px 16px; /* 내부 여백 포함 */
  }
  ```
- **본문 텍스트**: **최소 `font-size: 14px` 강제**, 권장 15~16px. 12~13px 는 데이터 라벨/배지/탭 칩 등 **비주요 정보 장식 목적** 에만 허용.
- **소형 텍스트 허용 범위 (예외)** — 차트 축 레이블, `uppercase letter-spacing` 된 섹션 레이블, badge/chip 텍스트, 표 셀의 부연 설명 (email 등): 11~13px 허용.
- **금지**: 본문 단락, 버튼 레이블, 테이블 데이터 셀, 메뉴 항목 등 **주요 정보** 는 절대 14px 미만 불가.
- **헤딩 최소** — h3 ≥ 18px, h2 ≥ 24px, h1 ≥ 32px
- **행간** — 본문 `line-height: 1.4~1.6` (너무 타이트 금지)
- **대비 (WCAG AA)** — 본문 텍스트/배경 대비 4.5:1 이상 (palette 설계 시 고려)

## 10. 컴포넌트 레벨 필수 (누락 빈도 높음)

LLM 이 전역 규칙 (§1~9) 은 준수해도 개별 컴포넌트 디테일을 빠뜨리는 빈도가 높아
**saver 가 결정론적 patch 로 baseline 주입**하지만, 최초 생성 시부터 아래를 포함할 것.

### 10-1. 체크박스 / 라디오 (터치 영역 + 레이블 포함)
```css
input[type="checkbox"], input[type="radio"] {
  min-width: 20px;
  min-height: 20px;
  accent-color: var(--accent);
}
/* 체크박스 단독은 20px 지만 label 전체가 터치 영역 44px 이 되도록 */
label:has(input[type="checkbox"]),
label:has(input[type="radio"]) {
  min-height: 44px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
}
```

### 10-2. 버튼 (터치 + 텍스트 wrap)
```css
button, .btn, [role="button"], input[type="submit"] {
  min-height: 44px;
  padding: 10px 16px;
  word-break: break-word;        /* 긴 한글/영문 잘림 방지 */
  overflow-wrap: anywhere;
  box-sizing: border-box;
}
/* white-space: nowrap 필요 시 ellipsis 동반 */
.btn-nowrap {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
```

### 10-3. 아이콘 래퍼 (정렬 + 크기 기준)
```css
.icon, .icon-wrap, .icon-box, [data-icon] {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;        /* 기본 크기 기준 */
  height: 24px;
  flex-shrink: 0;     /* flex 컨테이너 안에서 아이콘 뭉개지 방지 */
}
.icon svg { width: 100%; height: 100%; display: block; }
```

### 10-4. 테이블 모바일 reflow — **자동 처리 (saver patch)**

엔진 saver 가 **모든 `<table>` 에 자동으로 `<td data-label="...">` 주입** 하고
`@media (max-width: 600px)` 내에서 td::before content:attr(data-label) 로
**카드 reflow 기본 적용**. 별도 클래스 불필요.

LLM 이 지켜야 할 것:
- **`<thead><tr><th>헤더</th>...</tr></thead>` 제대로 선언** (saver 가 th 텍스트를 data-label 값으로 사용)
- 모바일에서 reflow 원치 않는 숫자 매트릭스/요약 행렬 테이블은 **`<table data-no-reflow>`** opt-out
- `white-space: nowrap` 은 특정 셀 (날짜, ID 등) 에만 제한적 사용. 남발 시 reflow 후에도 column 팽창 유발

처음부터 `<td data-label="이용자명">김민수</td>` 로 선언해도 됨 (saver 가 기존 label 을 유지, 덮어쓰지 않음 — 멱등).

**opt-out 예시 (숫자 매트릭스 등 reflow 부적합):**
```html
<table data-no-reflow>
  <thead><tr><th>월</th><th>화</th><th>수</th><th>목</th><th>금</th></tr></thead>
  <tbody><tr><td>1.2</td><td>3.4</td>...</tr></tbody>
</table>
```

### 10-5. section / card / panel 간격 (gap 우선)
```css
/* ❌ margin 개별 선언은 상쇄 이슈 + 반응형 불안정 */
/* ✅ 부모 container 에 gap 선언이 깔끔 */
.section-list, .card-grid, main > .stack {
  display: flex;
  flex-direction: column;
  gap: 24px;  /* 또는 clamp(16px, 4vw, 32px) 로 유동 */
}
```

**saver 결정론 baseline** (위 중 누락된 룰을 자동 보강):
`<style data-v10-responsive-patch='1'>` 블록이 `<head>` 시작부에 삽입되어 §10-1 ~ §10-4 의 최소 baseline + 테이블 카드 reflow 를 주입합니다. 프로젝트별 커스텀 (예: 특정 버튼을 60px 로 키움) 은 LLM 이 뒤 `<style>` 에 선언하면 cascade 로 override 됩니다 — **patch 는 빈자리만 채우는 fallback** 역할입니다.

## 11. 인터랙션 CSS 는 외부 `<style>` 블록에 정의 (`:hover` / `:focus` 강제)

인라인 `style=""` 속성에 `transition` 만 쓰고 `:hover` 정의가 없으면 **실제 hover 효과가 동작하지 않습니다** (CSS pseudo-class 는 inline style 에 선언 불가).

### 금지 패턴
```html
<!-- ❌ 이 row 는 hover 시 배경 변경 안 됨. transition 만 있고 :hover 정의 위치 없음 -->
<tr style="transition: background .15s">
```

### 올바른 패턴
```html
<style>
  .table-row { transition: background .15s; }
  .table-row:hover { background: rgba(255,255,255,0.04); }
  .table-row:focus-within { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
<tr class="table-row">...</tr>
```

### 금지 항목
- 인라인 `transition` / `animation` 만 있고 `<style>` 내 `:hover` / `:focus` / `:active` 정의 누락
- 버튼/링크/클릭 가능 요소에 시각 feedback (hover/focus) 미정의
- 키보드 포커스 가능 요소 (`<a>`, `<button>`, `<input>`, `[tabindex]`) 에 `:focus-visible` outline 제거 금지 (WCAG 2.4.7)

## 12. ARIA 접근성 필수 (스크린 리더 호환)

- **모든 form input 에 접근 가능한 이름** — `<label for="">` 또는 `aria-label` 또는 `aria-labelledby` 하나는 필수. `<input type="search" placeholder="...">` placeholder 만으론 부족 (spec 상 접근 가능 이름 아님).
- **아이콘 전용 버튼** — `<button>★</button>`, `<button>›</button>` 등 텍스트 없는 아이콘 버튼은 `aria-label="다음 페이지"` 필수. 스크린 리더 "버튼, 별" 대신 의미 전달.
- **`disabled` 버튼** — `<button disabled>` 는 DOM 레벨 disabled, HTML 속성으로 충분 (aria-disabled 별도 불필요). 단 `<div>` 를 가짜 버튼으로 쓰면 `role="button" aria-disabled="true" tabindex="-1"` 필수.
- **테이블 데이터 셀 data-label** — saver 가 자동 주입하지만 LLM 도 처음부터 선언 권장 (명확성). 모바일 reflow 시 header 문자열이 카드 라벨로 표시.
- **decorative 아이콘** — `<span>▲</span>` 같이 장식 목적의 문자/SVG 는 `aria-hidden="true"` 명시. 스크린 리더가 안 읽어야 의미 깨끗.
- **스크린 라벨 헤더** (SC-XX-NNN 형식) 는 `<header>` 로 감싸고 `aria-label` 로 의미 명시 권장.
