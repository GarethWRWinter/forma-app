"use client";

import { useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { safety, type SafetyState } from "@/lib/api";

/** One cache entry for the gate, shared by the hold banner, Settings, the FTP
    test card and onboarding, so lifting a hold anywhere clears it everywhere. */
export const SAFETY_STATE_KEY = ["safety-state"] as const;

export function useSafetyState(enabled = true) {
  return useQuery({
    queryKey: SAFETY_STATE_KEY,
    queryFn: () => safety.getState(),
    staleTime: 60 * 1000,
    retry: 1,
    enabled,
  });
}

/** Store a fresh state from any safety write, and let the plan views refetch:
    a hold opening or lifting changes what today's session should be. */
export function useApplySafetyState() {
  const queryClient = useQueryClient();
  return useCallback(
    (state: SafetyState) => {
      queryClient.setQueryData(SAFETY_STATE_KEY, state);
      for (const key of ["today-workouts", "today-workout-detail", "workouts-week", "plans"]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
    },
    [queryClient]
  );
}
