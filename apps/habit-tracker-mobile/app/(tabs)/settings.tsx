import React, { useEffect, useState } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  StyleSheet,
  Alert,
  Switch,
  ScrollView,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { getApiBase, setApiBase, getUserId } from "../../src/api/habits";
import { getPendingCount, flushPendingLogs } from "../../src/utils/offline";

export default function SettingsScreen() {
  const [apiBase, setApiBaseState] = useState("");
  const [userId, setUserId] = useState("");
  const [notify, setNotify] = useState(true);
  const [pendingCount, setPendingCount] = useState(0);

  useEffect(() => {
    (async () => {
      setApiBaseState(await getApiBase());
      setUserId(await getUserId());
      setPendingCount(await getPendingCount());
    })();
  }, []);

  const saveApiBase = async () => {
    if (!apiBase.startsWith("http")) {
      Alert.alert("오류", "http 또는 https 로 시작해야 합니다.");
      return;
    }
    await setApiBase(apiBase);
    Alert.alert("저장", "API 주소가 저장되었습니다.");
  };

  const doFlush = async () => {
    const n = await flushPendingLogs();
    setPendingCount(await getPendingCount());
    Alert.alert("동기화", `${n}건 동기화 완료`);
  };

  return (
    <SafeAreaView style={styles.container} edges={["top"]}>
      <ScrollView contentContainerStyle={styles.body}>
        <Text style={styles.title}>설정</Text>

        <Section title="서버">
          <Label>API 주소</Label>
          <TextInput
            style={styles.input}
            value={apiBase}
            onChangeText={setApiBaseState}
            placeholder="http://127.0.0.1:8004"
            autoCapitalize="none"
          />
          <Pressable style={styles.button} onPress={saveApiBase}>
            <Text style={styles.buttonText}>저장</Text>
          </Pressable>
          <Label>내 사용자 ID (자동 생성)</Label>
          <Text style={styles.readonly}>{userId}</Text>
        </Section>

        <Section title="동기화">
          <Row label="동기화 대기" value={`${pendingCount}건`} />
          <Pressable
            style={[styles.button, pendingCount === 0 && styles.buttonDisabled]}
            disabled={pendingCount === 0}
            onPress={doFlush}
          >
            <Text style={styles.buttonText}>지금 동기화</Text>
          </Pressable>
        </Section>

        <Section title="알림">
          <View style={styles.switchRow}>
            <Text style={styles.switchLabel}>맞춤 시간 알림</Text>
            <Switch value={notify} onValueChange={setNotify} />
          </View>
          <Text style={styles.hint}>
            (네이티브 알림은 expo-notifications 로 구현 예정)
          </Text>
        </Section>

        <Section title="정보">
          <Row label="버전" value="1.0.0" />
          <Row label="연동" value="V9 Engine + Phase F" />
        </Section>
      </ScrollView>
    </SafeAreaView>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      <View style={styles.sectionBody}>{children}</View>
    </View>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text style={styles.rowValue}>{value}</Text>
    </View>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <Text style={styles.label}>{children}</Text>;
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#FAF8F3" },
  body: { padding: 20 },
  title: { fontSize: 26, fontWeight: "700", marginBottom: 16 },
  section: { marginBottom: 20 },
  sectionTitle: { fontSize: 13, color: "#888", fontWeight: "600", marginBottom: 8, paddingHorizontal: 4 },
  sectionBody: { backgroundColor: "#fff", borderRadius: 12, padding: 16 },
  label: { fontSize: 13, color: "#666", marginBottom: 6, marginTop: 8 },
  input: {
    borderWidth: 1,
    borderColor: "#ddd",
    borderRadius: 8,
    padding: 10,
    fontSize: 14,
    backgroundColor: "#fafafa",
  },
  readonly: { fontSize: 14, color: "#555", padding: 10 },
  button: {
    backgroundColor: "#4CAF50",
    paddingVertical: 10,
    borderRadius: 8,
    alignItems: "center",
    marginTop: 10,
  },
  buttonDisabled: { backgroundColor: "#cccccc" },
  buttonText: { color: "#fff", fontSize: 14, fontWeight: "600" },
  row: {
    flexDirection: "row",
    justifyContent: "space-between",
    paddingVertical: 8,
  },
  rowLabel: { fontSize: 14, color: "#666" },
  rowValue: { fontSize: 14, color: "#1a1a1a", fontWeight: "500" },
  switchRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  switchLabel: { fontSize: 15, color: "#1a1a1a" },
  hint: { fontSize: 12, color: "#aaa", marginTop: 8 },
});
