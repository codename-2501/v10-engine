import React, { useCallback, useState } from "react";
import {
  View,
  Text,
  ScrollView,
  StyleSheet,
  ActivityIndicator,
  Pressable,
} from "react-native";
import { useFocusEffect } from "expo-router";
import { SafeAreaView } from "react-native-safe-area-context";
import { AnalysisCard } from "../../src/components/AnalysisCard";
import { listHabits, getAnalysis } from "../../src/api/habits";
import type { Habit, HabitAnalysis } from "../../src/types";

export default function AnalysisScreen() {
  const [habits, setHabits] = useState<Habit[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<HabitAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useFocusEffect(
    useCallback(() => {
      (async () => {
        setLoading(true);
        try {
          const list = await listHabits();
          setHabits(list);
          if (list.length > 0 && !selectedId) {
            setSelectedId(list[0].id);
          }
        } catch (e: any) {
          setError(e?.message || "서버 연결 실패");
        } finally {
          setLoading(false);
        }
      })();
    }, [selectedId])
  );

  useFocusEffect(
    useCallback(() => {
      if (!selectedId) return;
      (async () => {
        setLoading(true);
        try {
          const res = await getAnalysis(selectedId);
          setAnalysis(res);
          setError(null);
        } catch (e: any) {
          setError(e?.message || "분석 로드 실패");
        } finally {
          setLoading(false);
        }
      })();
    }, [selectedId])
  );

  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      <Text style={styles.title}>AI 분석</Text>
      <Text style={styles.subtitle}>선택한 습관의 패턴과 조언을 확인하세요</Text>

      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.tabs}
      >
        {habits.map((h) => (
          <Pressable
            key={h.id}
            onPress={() => setSelectedId(h.id)}
            style={[styles.tab, selectedId === h.id && styles.tabActive]}
          >
            <Text
              style={[styles.tabText, selectedId === h.id && styles.tabTextActive]}
              numberOfLines={1}
            >
              {h.name}
            </Text>
          </Pressable>
        ))}
      </ScrollView>

      <ScrollView contentContainerStyle={styles.body}>
        {loading && <ActivityIndicator style={{ marginTop: 30 }} />}
        {error && <Text style={styles.error}>{error}</Text>}
        {analysis && !loading && <AnalysisCard analysis={analysis} />}
        {!loading && habits.length === 0 && (
          <Text style={styles.empty}>분석할 습관이 없습니다.</Text>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#FAF8F3" },
  title: { fontSize: 26, fontWeight: "700", color: "#1a1a1a", paddingHorizontal: 20, paddingTop: 16 },
  subtitle: { fontSize: 13, color: "#888", paddingHorizontal: 20, marginBottom: 12 },
  tabs: { paddingHorizontal: 20, paddingVertical: 10, gap: 8 },
  tab: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    backgroundColor: "#fff",
    borderRadius: 20,
    maxWidth: 160,
  },
  tabActive: { backgroundColor: "#4CAF50" },
  tabText: { fontSize: 13, color: "#555" },
  tabTextActive: { color: "#fff", fontWeight: "600" },
  body: { padding: 20, paddingTop: 8 },
  error: { color: "#C43E1C", fontSize: 13, textAlign: "center", marginTop: 30 },
  empty: { color: "#888", textAlign: "center", marginTop: 40 },
});
