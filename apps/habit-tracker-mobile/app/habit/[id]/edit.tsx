import React from "react";
import { View, Text, StyleSheet } from "react-native";
import { useLocalSearchParams } from "expo-router";

/**
 * 습관 수정 화면.
 * V9 API에 PUT/PATCH 엔드포인트가 아직 없으므로 MVP로 placeholder.
 * 백엔드 확장 시 이 화면에서 createHabit 재활용.
 */
export default function EditHabitScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  return (
    <View style={styles.container}>
      <Text style={styles.title}>습관 수정</Text>
      <Text style={styles.body}>
        습관 ID: {id}
        {"\n\n"}
        수정 기능은 V9 API의 PATCH /habits/{"{id}"} 지원 후 활성화됩니다.
        {"\n"}
        현재는 조회/체크/AI 분석만 가능합니다.
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#FAF8F3", padding: 24, paddingTop: 40 },
  title: { fontSize: 22, fontWeight: "700", marginBottom: 16 },
  body: { fontSize: 14, color: "#555", lineHeight: 22 },
});
