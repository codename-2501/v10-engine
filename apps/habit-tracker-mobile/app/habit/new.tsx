import React, { useState } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  StyleSheet,
  Alert,
  ScrollView,
} from "react-native";
import { useRouter } from "expo-router";
import { createHabit } from "../../src/api/habits";
import type { HabitCategory } from "../../src/types";

const CATEGORIES: { v: HabitCategory; l: string; emoji: string }[] = [
  { v: "health", l: "건강", emoji: "💪" },
  { v: "learning", l: "학습", emoji: "📚" },
  { v: "productivity", l: "생산성", emoji: "⚡" },
  { v: "wellness", l: "웰빙", emoji: "🧘" },
  { v: "other", l: "기타", emoji: "✨" },
];

export default function NewHabitScreen() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [category, setCategory] = useState<HabitCategory>("health");
  const [targetDays, setTargetDays] = useState(3);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (!name.trim()) {
      Alert.alert("이름 필요", "습관 이름을 입력해주세요.");
      return;
    }
    setSubmitting(true);
    try {
      await createHabit(name.trim(), category, targetDays);
      router.back();
    } catch (e: any) {
      Alert.alert("오류", e?.message || "저장 실패");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      <Text style={styles.label}>습관 이름</Text>
      <TextInput
        style={styles.input}
        value={name}
        onChangeText={setName}
        placeholder="예: 아침 운동, 하루 30분 독서"
        autoFocus
      />

      <Text style={styles.label}>카테고리</Text>
      <View style={styles.grid}>
        {CATEGORIES.map((c) => (
          <Pressable
            key={c.v}
            style={[styles.chip, category === c.v && styles.chipOn]}
            onPress={() => setCategory(c.v)}
          >
            <Text style={styles.chipEmoji}>{c.emoji}</Text>
            <Text style={[styles.chipText, category === c.v && styles.chipTextOn]}>{c.l}</Text>
          </Pressable>
        ))}
      </View>

      <Text style={styles.label}>목표 (주 몇 회)</Text>
      <View style={styles.row}>
        {[1, 2, 3, 4, 5, 6, 7].map((n) => (
          <Pressable
            key={n}
            style={[styles.numBtn, targetDays === n && styles.numBtnOn]}
            onPress={() => setTargetDays(n)}
          >
            <Text style={[styles.numText, targetDays === n && styles.numTextOn]}>{n}</Text>
          </Pressable>
        ))}
      </View>

      <Pressable
        style={[styles.submit, submitting && styles.submitDisabled]}
        onPress={submit}
        disabled={submitting}
      >
        <Text style={styles.submitText}>{submitting ? "저장 중..." : "저장"}</Text>
      </Pressable>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#FAF8F3" },
  content: { padding: 20 },
  label: { fontSize: 13, color: "#666", fontWeight: "600", marginTop: 16, marginBottom: 8 },
  input: {
    backgroundColor: "#fff",
    padding: 14,
    borderRadius: 10,
    fontSize: 16,
    borderWidth: 1,
    borderColor: "#E8E6DF",
  },
  grid: { flexDirection: "row", flexWrap: "wrap", gap: 10 },
  chip: {
    paddingVertical: 10,
    paddingHorizontal: 14,
    borderRadius: 20,
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: "#E8E6DF",
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  chipOn: { backgroundColor: "#4CAF50", borderColor: "#4CAF50" },
  chipEmoji: { fontSize: 18 },
  chipText: { color: "#555", fontSize: 14 },
  chipTextOn: { color: "#fff", fontWeight: "600" },
  row: { flexDirection: "row", gap: 8 },
  numBtn: {
    width: 40,
    height: 40,
    borderRadius: 8,
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: "#E8E6DF",
    alignItems: "center",
    justifyContent: "center",
  },
  numBtnOn: { backgroundColor: "#4CAF50", borderColor: "#4CAF50" },
  numText: { color: "#555", fontSize: 15 },
  numTextOn: { color: "#fff", fontWeight: "700" },
  submit: {
    marginTop: 30,
    backgroundColor: "#4CAF50",
    padding: 14,
    borderRadius: 10,
    alignItems: "center",
  },
  submitDisabled: { backgroundColor: "#cccccc" },
  submitText: { color: "#fff", fontSize: 16, fontWeight: "600" },
});
