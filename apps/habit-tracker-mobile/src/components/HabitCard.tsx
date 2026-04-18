import React from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import type { Habit } from "../types";

interface Props {
  habit: Habit;
  onToggle?: () => void;
  onPress?: () => void;
  checked?: boolean;
}

const CATEGORY_EMOJI: Record<Habit["category"], string> = {
  health: "💪",
  learning: "📚",
  productivity: "⚡",
  wellness: "🧘",
  other: "✨",
};

export function HabitCard({ habit, onToggle, onPress, checked }: Props) {
  const rate = Math.round((habit.completion_rate ?? 0) * 100);
  return (
    <Pressable onPress={onPress} style={styles.card}>
      <View style={styles.left}>
        <Text style={styles.emoji}>{CATEGORY_EMOJI[habit.category] ?? "✨"}</Text>
        <View style={{ flex: 1 }}>
          <Text style={styles.name}>{habit.name}</Text>
          <Text style={styles.meta}>
            주 {habit.target_days}회 · streak {habit.streak ?? 0}일 · {rate}%
          </Text>
        </View>
      </View>
      <Pressable
        onPress={(e) => {
          e.stopPropagation?.();
          onToggle?.();
        }}
        style={[styles.check, checked && styles.checkOn]}
      >
        <Text style={styles.checkMark}>{checked ? "✓" : ""}</Text>
      </Pressable>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  card: {
    flexDirection: "row",
    alignItems: "center",
    padding: 16,
    backgroundColor: "#fff",
    borderRadius: 14,
    marginBottom: 10,
    shadowColor: "#000",
    shadowOpacity: 0.04,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 2 },
    elevation: 2,
  },
  left: { flex: 1, flexDirection: "row", alignItems: "center", gap: 12 },
  emoji: { fontSize: 28 },
  name: { fontSize: 17, fontWeight: "600", color: "#1a1a1a" },
  meta: { fontSize: 13, color: "#888", marginTop: 2 },
  check: {
    width: 34,
    height: 34,
    borderRadius: 17,
    borderWidth: 2,
    borderColor: "#d0d0d0",
    alignItems: "center",
    justifyContent: "center",
  },
  checkOn: { backgroundColor: "#4CAF50", borderColor: "#4CAF50" },
  checkMark: { color: "#fff", fontSize: 18, fontWeight: "700" },
});
