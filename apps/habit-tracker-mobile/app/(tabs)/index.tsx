import React, { useCallback, useEffect, useState } from "react";
import {
  View,
  Text,
  ScrollView,
  RefreshControl,
  StyleSheet,
  Pressable,
  ActivityIndicator,
  Alert,
} from "react-native";
import { useRouter, useFocusEffect } from "expo-router";
import { SafeAreaView } from "react-native-safe-area-context";
import { HabitCard } from "../../src/components/HabitCard";
import { listHabits, logExecution, getUserId } from "../../src/api/habits";
import { queuePendingLog, flushPendingLogs, getPendingCount } from "../../src/utils/offline";
import type { Habit } from "../../src/types";

export default function HabitListScreen() {
  const router = useRouter();
  const [habits, setHabits] = useState<Habit[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [checkedToday, setCheckedToday] = useState<Set<string>>(new Set());
  const [pendingCount, setPendingCount] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const list = await listHabits();
      setHabits(list);
      setPendingCount(await getPendingCount());
    } catch (e: any) {
      setError(e?.message || "서버 연결 실패");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      (async () => {
        // 화면 복귀 시 오프라인 큐 flush
        try {
          const flushed = await flushPendingLogs();
          if (flushed > 0) console.log(`[sync] ${flushed} logs flushed`);
        } catch {}
        load();
      })();
    }, [load])
  );

  useEffect(() => {
    load();
  }, [load]);

  const handleToggle = async (habit: Habit) => {
    if (checkedToday.has(habit.id)) return;
    setCheckedToday(new Set([...checkedToday, habit.id]));
    try {
      await logExecution(habit.id, "");
      load();
    } catch {
      // 오프라인 큐로 저장
      const uid = await getUserId();
      await queuePendingLog({
        habit_id: habit.id,
        user_id: uid,
        notes: "",
        queued_at: Date.now(),
      });
      setPendingCount(await getPendingCount());
      Alert.alert("오프라인 저장", "네트워크 복귀 시 자동 동기화됩니다.");
    }
  };

  if (loading) {
    return (
      <SafeAreaView style={styles.container}>
        <ActivityIndicator style={{ marginTop: 40 }} />
      </SafeAreaView>
    );
  }

  const total = habits.length;
  const doneToday = checkedToday.size;
  const percent = total === 0 ? 0 : Math.round((doneToday / total) * 100);

  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      <View style={styles.header}>
        <Text style={styles.date}>
          {new Date().toLocaleDateString("ko-KR", {
            month: "long",
            day: "numeric",
            weekday: "long",
          })}
        </Text>
        <Text style={styles.progress}>오늘 {doneToday}/{total} ({percent}%)</Text>
        {pendingCount > 0 && (
          <Text style={styles.pending}>⏳ 동기화 대기 {pendingCount}건</Text>
        )}
      </View>

      {error && (
        <View style={styles.errorBanner}>
          <Text style={styles.errorText}>⚠ {error}</Text>
        </View>
      )}

      <ScrollView
        contentContainerStyle={styles.list}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => { setRefreshing(true); load(); }} />}
      >
        {habits.length === 0 ? (
          <View style={styles.empty}>
            <Text style={styles.emptyText}>아직 습관이 없습니다.</Text>
            <Text style={styles.emptyHint}>+ 버튼으로 첫 습관을 등록해보세요.</Text>
          </View>
        ) : (
          habits.map((h) => (
            <HabitCard
              key={h.id}
              habit={h}
              checked={checkedToday.has(h.id)}
              onToggle={() => handleToggle(h)}
              onPress={() => router.push(`/habit/${h.id}`)}
            />
          ))
        )}
      </ScrollView>

      <Pressable style={styles.fab} onPress={() => router.push("/habit/new")}>
        <Text style={styles.fabText}>+</Text>
      </Pressable>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#FAF8F3" },
  header: { paddingHorizontal: 20, paddingVertical: 16 },
  date: { fontSize: 14, color: "#888" },
  progress: { fontSize: 26, fontWeight: "700", color: "#1a1a1a", marginTop: 4 },
  pending: { fontSize: 12, color: "#B8690A", marginTop: 4 },
  errorBanner: {
    backgroundColor: "#FFE5E0",
    padding: 12,
    marginHorizontal: 20,
    borderRadius: 8,
    marginBottom: 8,
  },
  errorText: { color: "#C43E1C", fontSize: 13 },
  list: { padding: 20, paddingTop: 0, paddingBottom: 100 },
  empty: { alignItems: "center", paddingVertical: 60 },
  emptyText: { fontSize: 16, color: "#666", marginBottom: 8 },
  emptyHint: { fontSize: 13, color: "#aaa" },
  fab: {
    position: "absolute",
    bottom: 24,
    right: 24,
    width: 56,
    height: 56,
    borderRadius: 28,
    backgroundColor: "#4CAF50",
    alignItems: "center",
    justifyContent: "center",
    shadowColor: "#000",
    shadowOpacity: 0.2,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 4 },
    elevation: 6,
  },
  fabText: { color: "#fff", fontSize: 30, fontWeight: "300" },
});
