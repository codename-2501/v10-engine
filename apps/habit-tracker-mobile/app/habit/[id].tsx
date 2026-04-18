import React, { useCallback, useState } from "react";
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  ActivityIndicator,
  Pressable,
} from "react-native";
import { useLocalSearchParams, useRouter, useFocusEffect } from "expo-router";
import { getAnalysis, listHabits } from "../../src/api/habits";
import type { Habit, HabitAnalysis } from "../../src/types";

export default function HabitDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const [habit, setHabit] = useState<Habit | null>(null);
  const [analysis, setAnalysis] = useState<HabitAnalysis | null>(null);
  const [loading, setLoading] = useState(true);

  useFocusEffect(
    useCallback(() => {
      if (!id) return;
      (async () => {
        try {
          const [list, a] = await Promise.all([listHabits(), getAnalysis(id)]);
          setHabit(list.find((h) => h.id === id) ?? null);
          setAnalysis(a);
        } finally {
          setLoading(false);
        }
      })();
    }, [id])
  );

  if (loading) return <ActivityIndicator style={{ marginTop: 40 }} />;
  if (!habit) return <Text style={{ padding: 20 }}>습관을 찾을 수 없습니다.</Text>;

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Text style={styles.name}>{habit.name}</Text>
      <Text style={styles.meta}>
        카테고리: {habit.category} · 주 {habit.target_days}회
      </Text>

      {/* 달력 플레이스홀더 (react-native-calendars 설치 시 확장) */}
      <View style={styles.calendar}>
        <Text style={styles.calendarTitle}>30일 달성 현황</Text>
        <View style={styles.grid}>
          {Array.from({ length: 30 }).map((_, i) => (
            <View
              key={i}
              style={[
                styles.day,
                i < (analysis?.streak ?? 0) && styles.dayDone,
              ]}
            />
          ))}
        </View>
      </View>

      <View style={styles.stats}>
        <StatLine label="연속 달성" value={`${analysis?.streak ?? 0}일`} />
        <StatLine
          label="이번 달 달성률"
          value={`${Math.round((analysis?.completion_rate ?? 0) * 100)}%`}
        />
        <StatLine
          label="가장 잘한 요일"
          value={analysis?.patterns?.best_day ?? "-"}
        />
        <StatLine
          label="가장 어려운 요일"
          value={analysis?.patterns?.worst_day ?? "-"}
        />
      </View>

      <Pressable style={styles.editBtn} onPress={() => router.push(`/habit/${id}/edit`)}>
        <Text style={styles.editText}>수정</Text>
      </Pressable>
    </ScrollView>
  );
}

function StatLine({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.statRow}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={styles.statValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#FAF8F3" },
  content: { padding: 20 },
  name: { fontSize: 28, fontWeight: "700", color: "#1a1a1a" },
  meta: { fontSize: 13, color: "#888", marginTop: 6, marginBottom: 20 },
  calendar: {
    backgroundColor: "#fff",
    padding: 16,
    borderRadius: 12,
    marginBottom: 16,
  },
  calendarTitle: { fontSize: 14, fontWeight: "600", color: "#555", marginBottom: 12 },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  day: {
    width: 26,
    height: 26,
    borderRadius: 6,
    backgroundColor: "#ECEAE0",
  },
  dayDone: { backgroundColor: "#4CAF50" },
  stats: {
    backgroundColor: "#fff",
    padding: 16,
    borderRadius: 12,
    marginBottom: 20,
  },
  statRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 8,
  },
  statLabel: { fontSize: 14, color: "#666" },
  statValue: { fontSize: 14, color: "#1a1a1a", fontWeight: "600" },
  editBtn: {
    backgroundColor: "#4CAF50",
    padding: 14,
    borderRadius: 10,
    alignItems: "center",
  },
  editText: { color: "#fff", fontSize: 15, fontWeight: "600" },
});
