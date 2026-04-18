# 마이 루틴 (habit-tracker-mobile)

V9 엔진의 Habit Tracker API 를 쓰는 Expo RN 모바일 앱.
iOS / Android / 웹 모두 동작.

## 구조

```
app/
  _layout.tsx              Stack + SafeArea 루트
  (tabs)/
    _layout.tsx           Bottom tabs (오늘 / 분석 / 설정)
    index.tsx             화면 1: HabitList
    analysis.tsx          화면 4: Analysis (AI 분석 + warnings)
    settings.tsx          화면 5: Settings (API 주소 / 동기화 / 알림)
  habit/
    new.tsx               화면 3-a: Add
    [id].tsx              화면 2: Detail (30일 달력 + 통계)
    [id]/edit.tsx         화면 3-b: Edit (placeholder)

src/
  api/habits.ts           V9 /habits API 래퍼 (Axios)
  components/             HabitCard, AnalysisCard
  utils/offline.ts        AsyncStorage 기반 오프라인 큐
  types/                  Habit, HabitLog, HabitAnalysis
```

## 실행

```bash
cd apps/habit-tracker-mobile
npm install
npx expo start --web        # 브라우저
# 또는
npx expo start              # QR로 Expo Go 실행
```

기본 API 주소: `http://127.0.0.1:8004` (V9 서버)
Settings 화면에서 변경 가능.

## V9 API 매핑

| 화면 | 호출 |
|------|------|
| HabitList | `GET /habits?user_id=<uid>` |
| HabitList 체크 | `POST /habits/{id}/log` (409 → 오프라인 큐) |
| New | `POST /habits` |
| Detail | `GET /habits/{id}/analysis?user_id=<uid>` |
| Analysis | `GET /habits/{id}/analysis?user_id=<uid>` |

## 오프라인 UX

네트워크 실패 시 log 를 AsyncStorage 에 큐잉 → 화면 복귀 시 자동 flush.
동기화 상태는 Settings 에서 수동 확인 가능.

## 한계

- react-native-calendars 미설치 (MVP 는 자체 30개 셀 그리드)
- edit 기능은 V9 PATCH 엔드포인트 후 활성화
- expo-notifications 플러그인 선언만, 스케줄 로직은 후속
