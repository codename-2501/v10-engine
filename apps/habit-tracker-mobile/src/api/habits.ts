import axios, { AxiosError } from "axios";
import AsyncStorage from "@react-native-async-storage/async-storage";
import Constants from "expo-constants";
import type { Habit, HabitLog, HabitAnalysis } from "../types";

const DEFAULT_BASE =
  (Constants.expoConfig?.extra as any)?.apiBase ||
  process.env.EXPO_PUBLIC_API_BASE ||
  "http://127.0.0.1:8004";

const API_BASE_KEY = "@mylifetracker/api_base";
const USER_ID_KEY = "@mylifetracker/user_id";

export async function getApiBase(): Promise<string> {
  return (await AsyncStorage.getItem(API_BASE_KEY)) || DEFAULT_BASE;
}

export async function setApiBase(url: string): Promise<void> {
  await AsyncStorage.setItem(API_BASE_KEY, url);
}

export async function getUserId(): Promise<string> {
  let uid = await AsyncStorage.getItem(USER_ID_KEY);
  if (!uid) {
    uid = `u-${Math.random().toString(36).slice(2, 10)}`;
    await AsyncStorage.setItem(USER_ID_KEY, uid);
  }
  return uid;
}

async function client() {
  const baseURL = await getApiBase();
  return axios.create({ baseURL, timeout: 10000 });
}

export async function listHabits(): Promise<Habit[]> {
  const c = await client();
  const uid = await getUserId();
  const res = await c.get("/habits", { params: { user_id: uid } });
  return res.data.habits ?? [];
}

export async function createHabit(
  name: string,
  category: Habit["category"],
  target_days: number
): Promise<Habit> {
  const c = await client();
  const uid = await getUserId();
  const res = await c.post("/habits", {
    user_id: uid,
    name,
    category,
    target_days,
  });
  return res.data;
}

export async function logExecution(
  habitId: string,
  notes: string = ""
): Promise<HabitLog> {
  const c = await client();
  const uid = await getUserId();
  try {
    const res = await c.post(`/habits/${habitId}/log`, {
      user_id: uid,
      notes,
    });
    return res.data;
  } catch (err) {
    const ax = err as AxiosError;
    if (ax.response?.status === 409) {
      // 중복 기록은 성공으로 간주 (이미 오늘 기록됨)
      return { log_id: "", habit_id: habitId, logged_at: new Date().toISOString().split("T")[0], notes };
    }
    throw err;
  }
}

export async function getAnalysis(habitId: string): Promise<HabitAnalysis> {
  const c = await client();
  const uid = await getUserId();
  const res = await c.get(`/habits/${habitId}/analysis`, {
    params: { user_id: uid },
  });
  return res.data;
}
