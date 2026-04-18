export type HabitCategory = "health" | "learning" | "productivity" | "wellness" | "other";

export interface Habit {
  id: string;
  user_id: string;
  name: string;
  category: HabitCategory;
  target_days: number;
  created_at: string;
  streak?: number;
  completion_rate?: number;
}

export interface HabitLog {
  log_id: string;
  habit_id: string;
  logged_at: string;
  notes: string;
}

export interface HabitAnalysis {
  habit_name: string;
  completion_rate: number;
  streak: number;
  patterns: {
    best_day: string;
    worst_day: string;
    total_logs?: number;
  };
  insights: string[];
  warnings: string[];
}

export interface PendingLog {
  habit_id: string;
  user_id: string;
  notes: string;
  queued_at: number;
}
