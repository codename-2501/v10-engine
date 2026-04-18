import React from "react";
import { View, Text, StyleSheet } from "react-native";
import type { HabitAnalysis } from "../types";

interface Props {
  analysis: HabitAnalysis;
}

export function AnalysisCard({ analysis }: Props) {
  const rate = Math.round((analysis.completion_rate ?? 0) * 100);
  return (
    <View style={styles.container}>
      <Text style={styles.title}>{analysis.habit_name}</Text>

      <View style={styles.statsRow}>
        <Stat label="달성률" value={`${rate}%`} />
        <Stat label="연속" value={`${analysis.streak}일`} />
        <Stat label="가장 잘한 요일" value={analysis.patterns.best_day} />
      </View>

      {analysis.warnings && analysis.warnings.length > 0 && (
        <View style={styles.warnings}>
          {analysis.warnings.map((w, i) => (
            <Text key={i} style={styles.warningText}>⚠️  {w}</Text>
          ))}
        </View>
      )}

      <Text style={styles.section}>AI 조언</Text>
      {(analysis.insights ?? []).map((line, i) => (
        <Text key={i} style={styles.insight}>· {line}</Text>
      ))}
    </View>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statValue}>{value}</Text>
      <Text style={styles.statLabel}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    padding: 20,
    backgroundColor: "#fff",
    borderRadius: 16,
    marginBottom: 16,
    shadowColor: "#000",
    shadowOpacity: 0.06,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 3 },
    elevation: 2,
  },
  title: { fontSize: 20, fontWeight: "700", color: "#1a1a1a", marginBottom: 16 },
  statsRow: { flexDirection: "row", gap: 12, marginBottom: 16 },
  stat: { flex: 1, padding: 12, backgroundColor: "#F5F5F0", borderRadius: 10 },
  statValue: { fontSize: 18, fontWeight: "700", color: "#1a1a1a" },
  statLabel: { fontSize: 12, color: "#888", marginTop: 4 },
  warnings: {
    padding: 12,
    backgroundColor: "#FFF6E5",
    borderRadius: 10,
    marginBottom: 12,
  },
  warningText: { color: "#B8690A", fontSize: 14, lineHeight: 20 },
  section: {
    fontSize: 14,
    fontWeight: "700",
    color: "#555",
    marginTop: 8,
    marginBottom: 6,
  },
  insight: { fontSize: 14, color: "#333", lineHeight: 22 },
});
