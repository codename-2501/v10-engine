import AsyncStorage from "@react-native-async-storage/async-storage";
import { logExecution } from "../api/habits";
import type { PendingLog } from "../types";

const QUEUE_KEY = "@mylifetracker/pending_logs";

export async function queuePendingLog(entry: PendingLog): Promise<void> {
  const raw = await AsyncStorage.getItem(QUEUE_KEY);
  const list: PendingLog[] = raw ? JSON.parse(raw) : [];
  list.push(entry);
  await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(list));
}

export async function flushPendingLogs(): Promise<number> {
  const raw = await AsyncStorage.getItem(QUEUE_KEY);
  if (!raw) return 0;
  const list: PendingLog[] = JSON.parse(raw);
  if (!list.length) return 0;

  const remaining: PendingLog[] = [];
  for (const entry of list) {
    try {
      await logExecution(entry.habit_id, entry.notes);
    } catch {
      remaining.push(entry); // 실패 시 재큐
    }
  }
  await AsyncStorage.setItem(QUEUE_KEY, JSON.stringify(remaining));
  return list.length - remaining.length;
}

export async function getPendingCount(): Promise<number> {
  const raw = await AsyncStorage.getItem(QUEUE_KEY);
  return raw ? JSON.parse(raw).length : 0;
}
