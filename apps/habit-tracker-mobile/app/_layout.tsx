import { Stack } from "expo-router";
import { SafeAreaProvider } from "react-native-safe-area-context";

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <Stack screenOptions={{ headerShown: false }}>
        <Stack.Screen name="(tabs)" />
        <Stack.Screen
          name="habit/new"
          options={{ presentation: "modal", headerShown: true, title: "새 습관" }}
        />
        <Stack.Screen
          name="habit/[id]"
          options={{ headerShown: true, title: "습관 상세" }}
        />
        <Stack.Screen
          name="habit/[id]/edit"
          options={{ presentation: "modal", headerShown: true, title: "습관 수정" }}
        />
      </Stack>
    </SafeAreaProvider>
  );
}
