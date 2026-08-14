/**
 * API client for the cycling coaching backend.
 * All requests go through Next.js rewrite proxy to http://localhost:8000.
 */

const API_BASE = "/api/v1";

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/**
 * Turn any FastAPI error body into a human sentence.
 *
 * FastAPI's `detail` is a string for HTTPExceptions but an ARRAY of
 * {loc, msg, ...} objects for 422 validation errors — rendering that
 * directly gives the user "[object Object]". Normalize every shape here
 * so forms always show a readable message.
 */
function normalizeErrorMessage(text: string): string {
  try {
    const json = JSON.parse(text);
    const detail = json.detail ?? json;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const msgs = detail
        .map((d) => {
          const field = Array.isArray(d?.loc) ? String(d.loc[d.loc.length - 1]) : "";
          const msg = typeof d?.msg === "string" ? d.msg : "";
          if (!msg) return "";
          return field && field !== "body" ? `${field}: ${msg}` : msg;
        })
        .filter(Boolean);
      if (msgs.length) return msgs.join(". ");
    }
    if (detail && typeof detail === "object" && typeof detail.msg === "string") {
      return detail.msg;
    }
  } catch {}
  return text || "Something went wrong. Try again.";
}

function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("access_token");
}

function setTokens(access: string, refresh: string) {
  localStorage.setItem("access_token", access);
  localStorage.setItem("refresh_token", refresh);
}

function clearTokens() {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });

  if (response.status === 401) {
    // Try refresh
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      headers["Authorization"] = `Bearer ${getToken()}`;
      const retry = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers,
      });
      if (!retry.ok) {
        throw new ApiError(normalizeErrorMessage(await retry.text()), retry.status);
      }
      return retry.json();
    }
    clearTokens();
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new ApiError("Unauthorized", 401);
  }

  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(normalizeErrorMessage(text), response.status);
  }

  if (response.status === 204) return null as T;
  return response.json();
}

// Single-flight: when a page opens with an expired access token, every query
// 401s at once. They must SHARE one refresh call — parallel refreshes replay
// the same rotating token, which the backend reads as theft and revokes the
// whole session (the "logged out every morning" bug).
let refreshInFlight: Promise<boolean> | null = null;

function tryRefreshToken(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    const refresh = localStorage.getItem("refresh_token");
    if (!refresh) return false;
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!res.ok) return false;
      const data = await res.json();
      setTokens(data.access_token, data.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      setTimeout(() => {
        refreshInFlight = null;
      }, 0);
    }
  })();
  return refreshInFlight;
}

async function uploadFile<T>(path: string, file: File): Promise<T> {
  const token = getToken();
  const formData = new FormData();
  formData.append("file", file);

  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers,
    body: formData,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(normalizeErrorMessage(text), response.status);
  }

  return response.json();
}

// === Auth ===

export const authConfig = () =>
  request<{ invite_required: boolean }>("/auth/config");

export const auth = {
  register: (email: string, password: string, fullName?: string, inviteCode?: string) =>
    request<{ id: string; email: string }>("/auth/register", {
      method: "POST",
      body: JSON.stringify({
        email,
        password,
        full_name: fullName,
        invite_code: inviteCode,
      }),
    }),

  login: async (email: string, password: string, rememberMe: boolean = true) => {
    const data = await request<{
      access_token: string;
      refresh_token: string;
    }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password, remember_me: rememberMe }),
    });
    setTokens(data.access_token, data.refresh_token);
    return data;
  },

  logout: () => {
    clearTokens();
    window.location.href = "/login";
  },

  isAuthenticated: () => !!getToken(),

  verifyEmail: (token: string) =>
    request<{ status: string }>("/auth/verify-email", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  resendVerification: () =>
    request<{ status: string }>("/auth/resend-verification", { method: "POST" }),

  forgotPassword: (email: string) =>
    request<{ status: string }>("/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),

  resetPassword: (token: string, newPassword: string) =>
    request<{ status: string }>("/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({ token, new_password: newPassword }),
    }),
};

// === Users ===

export interface UserProfile {
  id: string;
  email: string;
  email_verified?: boolean;
  founding_number?: number | null;
  full_name: string | null;
  weight_kg: number | null;
  height_cm: number | null;
  ftp: number | null;
  max_hr: number | null;
  resting_hr: number | null;
  experience_level: string | null;
  has_power_meter: boolean;
  has_smart_trainer: boolean;
  has_hr_monitor: boolean;
  weekly_hours_available: number | null;
  preferred_hard_days: number[] | null;
  rest_days: number[] | null;
  coach_name: string;
  coach_avatar: string;
  coach_tone: string;
}

export const users = {
  getProfile: () => request<UserProfile>("/users/me"),
  updateProfile: (data: Partial<UserProfile>) =>
    request<UserProfile>("/users/me", {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  getBadgePhoto: () =>
    request<{ data_url: string | null }>("/users/me/badge-photo"),
  setBadgePhoto: (dataUrl: string) =>
    request<{ saved: boolean }>("/users/me/badge-photo", {
      method: "PUT",
      body: JSON.stringify({ data_url: dataUrl }),
    }),
  clearBadgePhoto: () =>
    request<void>("/users/me/badge-photo", { method: "DELETE" }),
};

// === Rides ===

export interface Ride {
  id: string;
  title: string;
  ride_date: string;
  source: string;
  duration_seconds: number | null;
  distance_meters: number | null;
  elevation_gain_meters: number | null;
  average_power: number | null;
  normalized_power: number | null;
  max_power: number | null;
  average_hr: number | null;
  max_hr: number | null;
  average_cadence: number | null;
  average_speed: number | null;
  intensity_factor: number | null;
  variability_index: number | null;
  tss: number | null;
  ftp_at_time: number | null;
  calories: number | null;
  workout_id: string | null;
  achievement_count: number | null;
  pr_count: number | null;
  kudos_count: number | null;
  /** Cached time-in-zone: {"z": [s1..s7], "dom": "z3", "shape": [[h,z],...]} or {"none": true} */
  zone_summary?: {
    src?: "hr" | "elevation";
    profile?: number[];
    est_tss?: boolean;
    z?: number[];
    dom?: string;
    none?: boolean;
    shape?: [number, number][];
  } | null;
  /** Forma's name for the ride (falls back to source title) */
  forma_title?: string | null;
  title_is_custom?: boolean;
  location_name?: string | null;
  weather?: {
    none?: boolean;
    temp_c?: number | null;
    feels_c?: number | null;
    humidity?: number | null;
    wind_kph?: number | null;
    gust_kph?: number | null;
    wind_deg?: number | null;
    wind_dir?: string | null;
    pressure_hpa?: number | null;
    precip_mm?: number | null;
    condition?: string | null;
    description?: string | null;
  } | null;
  /** Forma's one-line story (falls back to the deterministic line) */
  story?: string | null;
}

export interface RideListResponse {
  rides: Ride[];
  total: number;
  page: number;
  per_page: number;
}

export interface RideDataPoint {
  elapsed_seconds: number;
  power: number | null;
  heart_rate: number | null;
  cadence: number | null;
  speed: number | null;
  distance: number | null;
  altitude: number | null;
  latitude?: number | null;
  longitude?: number | null;
}

export interface SegmentEffort {
  id: string;
  segment_name: string;
  distance_meters: number | null;
  average_grade: number | null;
  climb_category: number | null;
  elapsed_time_seconds: number;
  moving_time_seconds: number | null;
  average_watts: number | null;
  max_watts: number | null;
  average_hr: number | null;
  max_hr: number | null;
  pr_rank: number | null;
  kom_rank: number | null;
  achievement_type: string | null;
}

export interface RideSegmentsResponse {
  ride_id: string;
  achievement_count: number | null;
  pr_count: number | null;
  kudos_count: number | null;
  segment_efforts: SegmentEffort[];
}

export const rides = {
  list: (page = 1, perPage = 20) =>
    request<RideListResponse>(`/rides?page=${page}&per_page=${perPage}`),

  get: (id: string) => request<Ride>(`/rides/${id}`),

  update: (id: string, data: { title?: string; location_name?: string }) =>
    request<Ride>(`/rides/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  getData: (id: string, resolution = "5s") =>
    request<{ ride_id: string; data_points: RideDataPoint[]; resolution: string }>(
      `/rides/${id}/data?resolution=${resolution}`
    ),

  getPowerCurve: (id: string) =>
    request<{
      ride_id: string;
      points: { duration_seconds: number; duration_label: string; best_power: number }[];
    }>(`/rides/${id}/power-curve`),

  getSegments: (id: string) =>
    request<RideSegmentsResponse>(`/rides/${id}/segments`),

  upload: (file: File) => uploadFile<Ride>("/rides/upload", file),

  /** One file from a bulk archive import. Never throws on a bad file —
      the status field says what happened, so batches keep moving. */
  importFile: (name: string, bytes: Uint8Array) => {
    const blob = new Blob([bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer]);
    return uploadFile<{
      status: "imported" | "duplicate" | "failed" | "unsupported";
      filename: string;
      ride_id?: string;
      error?: string;
    }>("/rides/import-file", new File([blob], name));
  },

  /** After a bulk import: rebuild fitness history once from the earliest ride. */
  finalizeImport: () =>
    request<{ status: string; recalculated_from: string | null }>(
      "/rides/import-finalize",
      { method: "POST" }
    ),

  /**
   * Save an in-app workout recording (from the in-app session player).
   * Backend computes derived metrics (avg power, NP, IF, TSS, ...) and
   * triggers a Forma debrief in the background.
   */
  record: (body: {
    title: string;
    ride_date: string; // ISO datetime
    workout_id: string | null;
    data_points: RideDataPoint[];
  }) =>
    request<Ride>("/rides/record", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

// === Metrics ===

export interface PMCDataPoint {
  date: string;
  tss: number;
  ctl: number;
  atl: number;
  tsb: number;
  ramp_rate: number | null;
}

export interface PMCResponse {
  data: PMCDataPoint[];
  current_ctl: number;
  current_atl: number;
  current_tsb: number;
}

export interface RiderProfileScore {
  category: string;
  label: string;
  score: number;
  watts_per_kg?: number | null;
}

export interface FitnessSummary {
  ftp: number | null;
  w_per_kg: number | null;
  current_ctl: number;
  current_atl: number;
  current_tsb: number;
  ramp_rate: number;
  rider_type: string;
  strengths: string[];
  weaknesses: string[];
  fitness_level: string;
  profile_scores: RiderProfileScore[];
}

export interface ZonesResponse {
  ftp: number;
  power_zones: Record<string, { low: number; high: number }> | null;
  hr_zones: Record<string, { low: number; high: number }> | null;
}

export const metrics = {
  getPMC: (startDate?: string, endDate?: string) => {
    const params = new URLSearchParams();
    if (startDate) params.set("start_date", startDate);
    if (endDate) params.set("end_date", endDate);
    const qs = params.toString();
    return request<PMCResponse>(`/metrics/pmc${qs ? `?${qs}` : ""}`);
  },

  getFitnessSummary: (includePowerProfile = true) =>
    request<FitnessSummary>(`/metrics/fitness-summary?include_power_profile=${includePowerProfile}`),

  getZones: () => request<ZonesResponse>("/metrics/zones"),

  getPowerProfile: (days = 90) =>
    request<{
      points: {
        duration_seconds: number;
        duration_label: string;
        best_power: number;
        watts_per_kg: number | null;
        ride_id: string | null;
        ride_date: string | null;
        all_time_power: number | null;
        all_time_watts_per_kg: number | null;
        all_time_ride_date: string | null;
      }[];
      ftp: number;
      w_per_kg: number | null;
      days: number | null;
    }>(`/metrics/power-profile?days=${days}`),

  submitFTPTest: (twentyMinAvg: number) =>
    request<{ new_ftp: number; message: string }>("/metrics/ftp-test", {
      method: "POST",
      body: JSON.stringify({ twenty_min_avg_power: twentyMinAvg }),
    }),

  getWeeklyLoad: (weeks: number = 12) =>
    request<{
      weeks: {
        week_start: string;
        total_tss: number;
        ride_count: number;
        total_duration_seconds: number;
        avg_intensity_factor: number | null;
      }[];
    }>(`/metrics/weekly-load?weeks=${weeks}`),

  getRideZones: (rideId: string) =>
    request<{
      ftp: number;
      total_seconds: number;
      zones: {
        zone: string;
        zone_name: string;
        low_watts: number;
        high_watts: number;
        seconds: number;
        percentage: number;
      }[];
    }>(`/metrics/rides/${rideId}/zones`),

  getFTPHistory: () =>
    request<{
      history: { date: string; ftp: number; source: string }[];
      current_ftp: number | null;
    }>("/metrics/ftp-history"),
};

// === Training ===

export interface TrainingPlan {
  id: string;
  name: string;
  start_date: string;
  end_date: string;
  status: string;
  periodization_model: string;
  total_weeks: number;
  phase_count: number;
  phases?: TrainingPhase[];
}

export interface TrainingPhase {
  id: string;
  phase_type: string;
  start_date: string;
  end_date: string;
  focus: string | null;
  workout_count: number;
  sort_order: number;
}

export interface ActualRideSummary {
  id: string;
  title: string | null;
  ride_date: string;
  moving_time_seconds: number | null;
  duration_seconds: number | null;
  distance_meters: number | null;
  elevation_gain_meters: number | null;
  average_power: number | null;
  normalized_power: number | null;
  intensity_factor: number | null;
  tss: number | null;
  average_hr: number | null;
}

export interface WorkoutAdjustment {
  date?: string;
  workout_id?: string;
  change?: string;
  reason?: string;
}

export interface Workout {
  id: string;
  scheduled_date: string;
  title: string;
  description: string | null;
  workout_type: string;
  planned_duration_seconds: number | null;
  planned_tss: number | null;
  planned_if: number | null;
  status: string;
  actual_ride_id: string | null;
  actual_ride?: ActualRideSummary | null;
  execution_score?: number | null;
  execution_feedback?: string | null;
  execution_adjustments?: WorkoutAdjustment[] | null;
  execution_assessed_at?: string | null;
  steps?: WorkoutStep[];
}

export interface WorkoutAssessment {
  workout_id: string;
  score: number;
  feedback: string;
  adjustments: WorkoutAdjustment[];
  assessed_at: string | null;
}

export interface WorkoutStep {
  id: string;
  step_order: number;
  step_type: string;
  duration_seconds: number;
  power_target_pct: number | null;
  power_low_pct: number | null;
  power_high_pct: number | null;
  cadence_target: number | null;
  repeat_count: number | null;
  notes: string | null;
}

export const training = {
  getPlans: () =>
    request<{ plans: TrainingPlan[]; total: number }>("/plans"),

  generatePlan: (data: {
    goal_event_id?: string;
    periodization_model?: string;
    name?: string;
  }) =>
    request<TrainingPlan>("/plans/generate", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  getPlan: (id: string) => request<TrainingPlan>(`/plans/${id}`),

  getPlanWorkouts: (planId: string) =>
    request<{ plan_id: string; workouts: Workout[]; total: number }>(
      `/plans/${planId}/workouts`
    ),

  getWorkouts: (date?: string, week?: string) => {
    const params = new URLSearchParams();
    if (date) params.set("date", date);
    if (week) params.set("week", week);
    const qs = params.toString();
    return request<Workout[]>(`/workouts${qs ? `?${qs}` : ""}`);
  },

  getWorkout: (id: string) => request<Workout>(`/workouts/${id}`),

  updateWorkout: (id: string, data: { status?: string; actual_ride_id?: string }) =>
    request<Workout>(`/workouts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  getWorkoutAssessment: (id: string, force = false) =>
    request<WorkoutAssessment>(
      `/workouts/${id}/assessment${force ? "?force=true" : ""}`
    ),
};

// === Onboarding ===

export const onboarding = {
  getStatus: () =>
    request<{ completed: boolean; primary_goal: string | null }>("/onboarding/status"),

  submitQuiz: (data: {
    primary_goal: string;
    secondary_goals?: string[];
    current_weekly_volume_hours?: number;
    years_cycling?: number;
    indoor_outdoor_preference?: string;
  }) =>
    request("/onboarding/quiz", {
      method: "POST",
      body: JSON.stringify(data),
    }),
};

// === Goals ===

export interface RouteData {
  source: string;
  type?: string;
  url?: string;
  title?: string;
  description?: string;
  name?: string;
  total_distance_km?: number;
  elevation_gain_m?: number;
  min_elevation_m?: number;
  max_elevation_m?: number;
  avg_gradient_pct?: number;
  trackpoints?: number;
  elevation_profile?: Array<{
    distance_km: number;
    elevation_m: number;
    gradient_pct?: number;
  }>;
  error?: string;
}

export interface GoalEvent {
  id: string;
  event_name: string;
  event_date: string;
  event_type: string;
  priority: string;
  target_duration_minutes: number | null;
  notes: string | null;
  why: string | null;
  becoming: string | null;
  coach_read: string | null;
  coach_read_at: string | null;
  days_until: number | null;
  route_url: string | null;
  gpx_file_path: string | null;
  route_data: RouteData | null;
  status: string;
  actual_ride_id: string | null;
  finish_time_seconds: number | null;
  finish_position: number | null;
  finish_position_total: number | null;
  overall_satisfaction: number | null;
  perceived_exertion: number | null;
  assessment_data: Record<string, unknown> | null;
  assessment_completed_at: string | null;
  needs_assessment: boolean;
}

export interface GoalAssessmentSubmit {
  status: string;
  actual_ride_id?: string | null;
  finish_time_seconds?: number | null;
  finish_position?: number | null;
  finish_position_total?: number | null;
  overall_satisfaction?: number | null;
  perceived_exertion?: number | null;
  assessment_data?: Record<string, unknown> | null;
}

export interface PlannedVsActual {
  target_time_seconds: number | null;
  actual_time_seconds: number | null;
  target_np: number | null;
  actual_np: number | null;
  actual_if: number | null;
  actual_vi: number | null;
  actual_tss: number | null;
  pacing_analysis: string | null;
  time_delta_seconds: number | null;
  time_delta_pct: number | null;
}

export interface CandidateRide {
  id: string;
  title: string;
  ride_date: string;
  duration_seconds: number | null;
  distance_meters: number | null;
  normalized_power: number | null;
  tss: number | null;
  average_power: number | null;
}

// --- Race Day Projection ---

export interface PerformanceEstimate {
  estimated_time_seconds: number;
  avg_power_watts: number;
  avg_speed_kph: number;
  ftp_used: number;
  ctl_used: number;
}

export interface PacingSegment {
  distance_km: number;
  elevation_m: number;
  gradient_pct: number;
  target_power_watts: number;
  target_power_pct_ftp: number;
  estimated_speed_kph: number;
  zone: string;
  zone_name: string;
}

export interface FitnessTrajectoryPoint {
  date: string;
  ctl: number;
  ftp: number;
  label: string | null;
}

export interface PerformanceImprovement {
  time_saved_seconds: number;
  speed_gain_kph: number;
  ftp_gain_watts: number;
}

export interface RaceProjection {
  current_performance: PerformanceEstimate;
  projected_performance: PerformanceEstimate | null;
  improvement: PerformanceImprovement | null;
  pacing_strategy: PacingSegment[];
  fitness_trajectory: FitnessTrajectoryPoint[];
}

export interface GoalReadiness {
  goal_id: string;
  current_ctl: number;
  target_ctl: number;
  current_ftp: number | null;
  w_per_kg: number | null;
  current_tsb: number;
  projected_tsb_on_event: number | null;
  days_until: number;
  readiness_score: number;
  readiness_label: string;
  recommendations: string[];
}

export const goals = {
  list: () => request<{ goals: GoalEvent[]; total: number }>("/goals"),

  get: (id: string) => request<GoalEvent>(`/goals/${id}`),

  create: (data: {
    event_name: string;
    event_date: string;
    event_type: string;
    priority: string;
    target_duration_minutes?: number;
    notes?: string;
    route_url?: string;
  }) =>
    request<GoalEvent>("/goals", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  update: (id: string, data: {
    event_name?: string;
    event_date?: string;
    event_type?: string;
    priority?: string;
    target_duration_minutes?: number | null;
    notes?: string | null;
    route_url?: string | null;
  }) =>
    request<GoalEvent>(`/goals/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  uploadGpx: (goalId: string, file: File) =>
    uploadFile<GoalEvent>(`/goals/${goalId}/gpx`, file),

  deleteGpx: (goalId: string) =>
    request<GoalEvent>(`/goals/${goalId}/gpx`, { method: "DELETE" }),

  delete: (id: string) =>
    request(`/goals/${id}`, { method: "DELETE" }),

  getReadiness: (id: string) =>
    request<GoalReadiness>(`/goals/${id}/readiness`),

  reparseGpx: (id: string) =>
    request<GoalEvent>(`/goals/${id}/reparse-gpx`, { method: "POST" }),

  coachRead: (id: string) =>
    request<GoalEvent>(`/goals/${id}/coach-read`, { method: "POST" }),

  refetchRoute: (id: string) =>
    request<GoalEvent>(`/goals/${id}/refetch-route`, { method: "POST" }),

  getRaceProjection: (id: string) =>
    request<RaceProjection>(`/goals/${id}/race-projection`),

  submitAssessment: (id: string, data: GoalAssessmentSubmit) =>
    request<GoalEvent>(`/goals/${id}/assessment`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  getAssessment: (id: string) =>
    request<{ goal: GoalEvent; planned_vs_actual: PlannedVsActual | null }>(
      `/goals/${id}/assessment`
    ),

  getCandidateRides: (id: string) =>
    request<{ rides: CandidateRide[] }>(`/goals/${id}/candidate-rides`),

  needsAssessment: () =>
    request<{ goals: GoalEvent[]; total: number }>("/goals/needs-assessment"),
};

// === Daily inspiration ===

export interface DailyInspiration {
  text: string;
  author: string;
  context?: string;
  detail?: string;
  tag: "quote" | "wisdom";
  date: string;
}

export const inspiration = {
  today: () => request<DailyInspiration>("/inspiration/today"),
};

// === Palmarès ===

export interface PalmaresGoal {
  id: string;
  name: string;
  date: string;
  year: number | null;
  priority: string;
  event_type: string;
  status: string;
  achieved: boolean;
  satisfaction: number | null;
  assessed: boolean;
}

export interface PalmaresRecord {
  key: string;
  label: string;
  value: string;
  unit: string;
  detail: string;
}

export interface Palmares {
  goals: PalmaresGoal[];
  records: PalmaresRecord[];
  totals: { rides: number; km: number; hours: number };
  milestones: { key: string; text: string }[];
  segment_prs: {
    name: string;
    time_seconds: number;
    date: string | null;
    distance_m: number | null;
  }[];
}

export const palmares = {
  get: () => request<Palmares>("/palmares"),
};

// === Chat ===

export interface ChatSession {
  id: string;
  title: string | null;
  pinned?: boolean;
  starred?: boolean;
  archived_at?: string | null;
  message_count: number;
  created_at: string;
  updated_at?: string | null;
}

export interface ChatSessionUpdate {
  title?: string;
  pinned?: boolean;
  starred?: boolean;
  archived?: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

/**
 * What the parser found in an attached ride file. The named fields are the
 * ones the composer chip reads; the index signature keeps the rest, because
 * the coach uses far more of the summary than the chip ever shows.
 */
export interface ChatAttachmentSummary {
  distance_km?: number;
  total_distance?: number;
  elevation_gain_m?: number;
  total_ascent?: number;
  [key: string]: unknown;
}

export interface ChatAttachment {
  id: string;
  filename: string;
  kind: string;
  summary?: ChatAttachmentSummary | null;
}

export const chat = {
  getSessions: (includeArchived?: boolean) =>
    request<ChatSession[]>(
      `/chat/sessions${includeArchived ? "?include_archived=true" : ""}`
    ),

  createSession: (title?: string) =>
    request<ChatSession>("/chat/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),

  updateSession: (id: string, update: ChatSessionUpdate) =>
    request<ChatSession>(`/chat/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(update),
    }),

  deleteSession: (id: string) =>
    request<null>(`/chat/sessions/${id}`, { method: "DELETE" }),

  getSession: (id: string, opts?: { limit?: number; before?: string }) => {
    const p = new URLSearchParams();
    if (opts?.limit) p.set("limit", String(opts.limit));
    if (opts?.before) p.set("before", opts.before);
    const qs = p.toString();
    return request<ChatSession & { messages: ChatMessage[]; has_more: boolean }>(
      `/chat/sessions/${id}${qs ? `?${qs}` : ""}`
    );
  },

  /**
   * Hand a ride file to the coach. Multipart, so the Content-Type header is
   * left alone deliberately: the browser has to write its own boundary.
   */
  uploadAttachment: (file: File) =>
    uploadFile<ChatAttachment>("/chat/attachments", file),

  sendMessage: async function* (
    sessionId: string,
    content: string,
    attachmentIds?: string[]
  ) {
    const token = getToken();
    const response = await fetch(`${API_BASE}/chat/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        content,
        ...(attachmentIds?.length ? { attachment_ids: attachmentIds } : {}),
      }),
    });

    if (!response.ok || !response.body) {
      throw new ApiError("Failed to send message", response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const data = JSON.parse(line.slice(6));
            yield data;
          } catch {}
        }
      }
    }
  },

  /**
   * Send a voice message — receives both text and audio chunks via SSE.
   * Audio chunks are base64-encoded MP3 for each sentence.
   */
  sendVoiceMessage: async function* (
    sessionId: string,
    content: string,
    attachmentIds?: string[]
  ) {
    const token = getToken();
    const response = await fetch(
      `${API_BASE}/chat/sessions/${sessionId}/voice-message`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          content,
          ...(attachmentIds?.length ? { attachment_ids: attachmentIds } : {}),
        }),
      }
    );

    if (!response.ok || !response.body) {
      throw new ApiError("Failed to send voice message", response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const data = JSON.parse(line.slice(6));
            yield data; // yields {type: "text"|"audio"|"done", ...}
          } catch {}
        }
      }
    }
  },

  /** Convert text to speech. Returns audio blob for playback. */
  textToSpeech: async (text: string): Promise<Blob> => {
    const token = getToken();
    const response = await fetch(`${API_BASE}/chat/tts`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      throw new ApiError("TTS failed", response.status);
    }
    return response.blob();
  },
};

// === Strava ===

export interface StravaBackfillStatus {
  status: string | null; // null, "running", "paused", "completed", "failed"
  total: number | null;
  progress: number;
  started_at: string | null;
  completed_at: string | null;
}

export interface StravaProbe {
  connected: boolean;
  ok: boolean;
  http_status?: number | null;
  stored_scope?: string | null;
  reason?: string | null; // "missing_activity_scope" | "token_invalid" | ...
}

export interface StravaStatus {
  connected: boolean;
  athlete_id?: number;
  last_sync_at?: string;
  token_expired?: boolean;
  scope?: string | null;
  can_read_activities?: boolean;
  backfill?: StravaBackfillStatus;
  probe?: StravaProbe;
}

export const strava = {
  getAuthUrl: () => request<{ auth_url: string }>("/integrations/strava/auth-url"),
  getStatus: (probe?: boolean) =>
    request<StravaStatus>(`/integrations/strava/status${probe ? "?probe=true" : ""}`),
  sync: () =>
    request<{
      synced: number;
      rides?: { id: string; title: string; date: string }[];
    }>("/integrations/strava/sync", { method: "POST" }),
  /**
   * Trigger the historical backfill.
   *
   * @param force - Force restart even if a backfill is already running.
   * @param sinceYears - Only import rides newer than this many years.
   *   Defaults to 2 (keeps the DB lean after a fresh start). Pass `null`
   *   explicitly to import full history.
   */
  startBackfill: (force?: boolean, sinceYears: number | null = 2) => {
    const params = new URLSearchParams();
    if (force) params.set("force", "true");
    if (sinceYears != null) params.set("since_years", String(sinceYears));
    const qs = params.toString();
    return request<{ status: string; since?: string | null }>(
      `/integrations/strava/backfill${qs ? `?${qs}` : ""}`,
      { method: "POST" }
    );
  },
  backfillSegments: () =>
    request<{ status: string }>("/integrations/strava/backfill-segments", { method: "POST" }),
  disconnect: () => request("/integrations/strava", { method: "DELETE" }),
};

// === Billing ===

export interface BillingStatus {
  configured: boolean;
  required: boolean;
  status: string; // "none" | "active" | "trialing" | "past_due" | "canceled"
  period_end: string | null;
  has_access: boolean;
}

export const billing = {
  getStatus: () => request<BillingStatus>("/billing/status"),
  checkout: () => request<{ url: string }>("/billing/checkout", { method: "POST" }),
  portal: () => request<{ url: string }>("/billing/portal", { method: "POST" }),
};

// === Wahoo ===

export interface WahooStatus {
  connected: boolean;
  configured: boolean;
  wahoo_user_id?: number | null;
  last_sync_at?: string | null;
  backfill?: {
    status: string; // "running" | "completed" | "failed"
    progress: number;
    total: number | null;
  };
}

export const wahoo = {
  getAuthUrl: () => request<{ auth_url: string }>("/integrations/wahoo/auth-url"),
  getStatus: () => request<WahooStatus>("/integrations/wahoo/status"),
  sync: () =>
    request<{
      synced: number;
      rides?: { id: string; title: string; date: string }[];
    }>("/integrations/wahoo/sync", { method: "POST" }),
  startBackfill: () =>
    request<{ status: string }>("/integrations/wahoo/backfill", { method: "POST" }),
  disconnect: () => request("/integrations/wahoo", { method: "DELETE" }),
};

// === Dropbox ===

export const dropbox = {
  getAuthUrl: () =>
    request<{ auth_url: string }>("/integrations/dropbox/auth-url"),

  getStatus: () =>
    request<{
      connected: boolean;
      account_id?: string;
      folder_path?: string;
      last_sync_at?: string;
    }>("/integrations/dropbox/status"),

  sync: () =>
    request<{ synced: number; rides: { id: string; title: string; date: string }[] }>(
      "/integrations/dropbox/sync",
      { method: "POST" }
    ),

  updateFolder: (folderPath: string) =>
    request("/integrations/dropbox/folder", {
      method: "PATCH",
      body: JSON.stringify({ folder_path: folderPath }),
    }),

  disconnect: () =>
    request("/integrations/dropbox", { method: "DELETE" }),
};

// === Exports ===

async function downloadFile(path: string, filename: string): Promise<void> {
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  let response = await fetch(`${API_BASE}${path}`, { headers });

  if (response.status === 401) {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      headers["Authorization"] = `Bearer ${getToken()}`;
      response = await fetch(`${API_BASE}${path}`, { headers });
    } else {
      clearTokens();
      if (typeof window !== "undefined") window.location.href = "/login";
      throw new ApiError("Unauthorized", 401);
    }
  }

  if (!response.ok) {
    throw new ApiError(
      `Download failed: ${response.statusText}`,
      response.status
    );
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// --- Coach Insights (Forma's proactive presence) ---

export interface CoachNudge {
  nudge: string;
  generated_at: string;
  cached: boolean;
}

export interface RideDebrief {
  debrief: string;
  generated_at: string;
  cached: boolean;
}

export interface MetricExplanation {
  explanation: string;
}

export interface BriefingConditions {
  at?: string;
  temp_c?: number | null;
  wind_kph?: number | null;
  gust_kph?: number | null;
  wind_dir?: string | null;
  rain_chance?: number;
  condition?: string | null;
}

export interface CoachBriefing {
  kind: "daily" | "goal";
  date: string;
  content: string;
  conditions: {
    now?: BriefingConditions | null;
    day?: {
      min_c?: number | null;
      max_c?: number | null;
      rain_chance?: number;
      sunrise?: string | null;
      sunset?: string | null;
    } | null;
    route?: {
      track: [number, number][];
      km: number[];
      segments: { from_km: number; to_km: number; wind: "head" | "tail" | "cross" }[];
      wind_deg?: number | null;
      wind_kph?: number | null;
    } | null;
  } | null;
}

export const coachInsights = {
  getNudge: () => request<CoachNudge>("/coach/nudge"),
  getBriefing: () => request<CoachBriefing>("/coach/briefing"),

  getRideDebrief: (rideId: string, force = false) =>
    request<RideDebrief>(`/coach/ride-debrief/${rideId}${force ? "?force=true" : ""}`),

  explainMetric: (metricName: string, metricValue: string | number) =>
    request<MetricExplanation>("/coach/explain", {
      method: "POST",
      body: JSON.stringify({ metric_name: metricName, metric_value: String(metricValue) }),
    }),
};

export const exports_ = {
  downloadZWO: (workoutId: string, title?: string) =>
    downloadFile(
      `/exports/workout/${workoutId}/zwo`,
      `${(title || "workout").replace(/\s+/g, "_")}.zwo`
    ),

  downloadERG: (workoutId: string, title?: string) =>
    downloadFile(
      `/exports/workout/${workoutId}/erg`,
      `${(title || "workout").replace(/\s+/g, "_")}.erg`
    ),

  downloadMRC: (workoutId: string, title?: string) =>
    downloadFile(
      `/exports/workout/${workoutId}/mrc`,
      `${(title || "workout").replace(/\s+/g, "_")}.mrc`
    ),

  downloadFIT: (workoutId: string, title?: string) =>
    downloadFile(
      `/exports/workout/${workoutId}/fit`,
      `${(title || "workout").replace(/\s+/g, "_")}.fit`
    ),

  downloadGPX: (rideId: string, title?: string) =>
    downloadFile(
      `/exports/ride/${rideId}/gpx`,
      `${(title || "ride").replace(/\s+/g, "_")}.gpx`
    ),
};

// === Memory — the Brain (Pillar 2) ===

export interface MemoryEntity {
  id: string;
  type: string;
  kind: string | null;
  life_area: string;
  label: string;
  summary: string | null;
  status: string | null;
  source: string | null;
  source_ref: string | null;
  observed_at: string | null;
  created_at: string | null;
}

export interface MemoryEdge {
  id: string;
  from_id: string;
  to_id: string;
  edge_type: string;
}

export interface MemoryGraph {
  entities: MemoryEntity[];
  edges: MemoryEdge[];
}

export const memory = {
  getGraph: () => request<MemoryGraph>("/memory/graph"),
  getReading: () => request<{ reading: string | null }>("/memory/reading"),
  hide: (id: string) =>
    request<{ id: string; hidden: boolean }>(`/memory/${id}/hide`, { method: "PATCH" }),
  unhide: (id: string) =>
    request<{ id: string; hidden: boolean }>(`/memory/${id}/unhide`, { method: "PATCH" }),
};
