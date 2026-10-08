"use client";

import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { authConfig, type ProfileConsentInput, type UserProfile } from "@/lib/api";
import { Input } from "@/components/ui/input";
import {
  BLOCKED_COUNTRIES,
  COUNTRIES,
  COUNTRY_HINT,
  DOB_HINT,
  ELSEWHERE,
} from "@/components/safety/safety-rules";
import {
  allowedCountriesFrom,
  countryBlockFor,
  countryOptions,
  type CountryBlock,
} from "@/lib/safetyPrompts";
import { countryClosedText } from "@/lib/accountExits";
import {
  PROFILE_CONSENT_INTRO,
  PROFILE_COUNTRY_LABEL,
  PROFILE_DOB_LABEL,
  profileConsentDue,
  profileErrorField,
  profilePayload,
  profileProblems,
  type ProfileChoices,
  type ProfileProblems,
} from "@/lib/profileConsent";

// The select shares the kit Input look (Input covers <input> only), as on
// the sign-up page.
const selectClasses =
  "flex h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text focus-visible:border-vb-red focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-vb-red disabled:cursor-not-allowed disabled:opacity-50";

/** "United Kingdom" for "GB", from the browser, for a code the server allows
    that the form has no name for. */
function regionName(code: string): string | null {
  try {
    return new Intl.DisplayNames(["en-GB"], { type: "region" }).of(code) ?? null;
  } catch {
    return null;
  }
}

/** Today as YYYY-MM-DD in the rider's own time zone, for the date picker's max. */
function todayISO(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export interface ProfileConsent {
  /** The server asked for a date of birth and a country (needs_profile_consent). */
  due: boolean;
  /** Shows the sentence under each field that stops the modal sending.
      True when it can send (always true when nothing is due). */
  check: () => boolean;
  /** What auth.reacceptTerms carries; undefined when nothing is due. */
  payload: ProfileConsentInput | undefined;
  /** Puts a refusal from the server (under 18, a country Forma doesn't serve)
      under its field. False when the message isn't about either field. */
  showServerError: (message: string) => boolean;
  /** The rider picked somewhere Forma isn't open: "us_ca" or "elsewhere",
      else null. The modal then stops offering to agree and turns to the
      ways out (Manage billing, Download my data, Delete my account). */
  countryClosed: CountryBlock;
  /** Spread onto <ProfileConsentFields />. */
  fieldProps: ProfileConsentFieldsProps;
}

/** A refusal from the server that says Forma isn't open where the rider
    lives (auth._not_available), for when its list and the form's disagree. */
function isNotAvailable(message: string): boolean {
  return /isn't available in\b/i.test(message);
}

/**
 * The date of birth and country the re-acceptance modal asks for when
 * /users/me says needs_profile_consent. Call it on every render, before any
 * early return; it reads GET /auth/config for the countries only while due.
 */
export function useProfileConsent(
  user: Pick<UserProfile, "needs_profile_consent"> | null | undefined
): ProfileConsent {
  const due = profileConsentDue(user);
  const [value, setValue] = useState<ProfileChoices>({ dateOfBirth: "", country: "" });
  const [problems, setProblems] = useState<ProfileProblems>({});
  // The server refused the country picked, though the form's list allowed it.
  const [serverClosed, setServerClosed] = useState(false);

  // The same list, and the same cache entry, as the sign-up form: the server
  // enforces it on this call too.
  const { data: config, isPending } = useQuery({
    queryKey: ["auth-config"],
    queryFn: () => authConfig(),
    enabled: due,
  });
  const allowed = useMemo(
    () => allowedCountriesFrom(config?.allowed_countries),
    [config?.allowed_countries]
  );
  const allowedCodes = useMemo(
    () => (allowed ? new Set(allowed.map((c) => c.code)) : null),
    [allowed]
  );
  const countries = useMemo(
    () => countryOptions(allowed, COUNTRIES, BLOCKED_COUNTRIES, regionName),
    [allowed]
  );

  // Somewhere off the server's list (or, without one, the US, Canada or
  // "Somewhere else") is answered here, as on the sign-up page: why, and
  // where to go instead, the moment it is picked.
  const closedFor = useCallback(
    (country: string): CountryBlock =>
      countryBlockFor(country, allowedCodes, BLOCKED_COUNTRIES, ELSEWHERE),
    [allowedCodes]
  );
  const picked = due ? closedFor(value.country) : null;
  const countryClosed: CountryBlock = picked ?? (due && serverClosed ? "elsewhere" : null);

  const check = useCallback(() => {
    if (!due) return true;
    const found = profileProblems(value, ELSEWHERE);
    const closed = closedFor(value.country);
    if (closed) found.country = countryClosedText(closed);
    setProblems(found);
    return !found.dateOfBirth && !found.country;
  }, [due, value, closedFor]);

  const showServerError = useCallback(
    (message: string) => {
      if (!due) return false;
      const field = profileErrorField(message);
      if (!field) return false;
      if (field === "country" && isNotAvailable(message)) {
        setServerClosed(true);
        setProblems((p) => ({ ...p, country: `${message} What you can do instead is below.` }));
        return true;
      }
      setProblems((p) => ({ ...p, [field]: message }));
      return true;
    },
    [due]
  );

  const onChange = useCallback(
    (field: keyof ProfileChoices, next: string) => {
      setValue((v) => ({ ...v, [field]: next }));
      if (field === "country") {
        setServerClosed(false);
        const closed = closedFor(next);
        setProblems((p) => ({ ...p, country: closed ? countryClosedText(closed) : undefined }));
        return;
      }
      setProblems((p) => ({ ...p, [field]: undefined }));
    },
    [closedFor]
  );

  return {
    due,
    check,
    payload: profilePayload(due, value),
    showServerError,
    countryClosed,
    fieldProps: {
      value,
      onChange,
      problems,
      countries,
      loadingCountries: due && isPending,
    },
  };
}

export interface ProfileConsentFieldsProps {
  value: ProfileChoices;
  onChange: (field: keyof ProfileChoices, next: string) => void;
  problems: ProfileProblems;
  countries: { code: string; name: string }[];
  loadingCountries: boolean;
  disabled?: boolean;
  className?: string;
}

/** Date of birth and where they live, laid out as on the sign-up page. */
export function ProfileConsentFields({
  value,
  onChange,
  problems,
  countries,
  loadingCountries,
  disabled,
  className,
}: ProfileConsentFieldsProps) {
  return (
    <div className={className}>
      <p className="text-sm leading-relaxed text-vb-text-dim">{PROFILE_CONSENT_INTRO}</p>

      <div className="mt-4 space-y-4">
        <div>
          <label htmlFor="profile-dob" className="f-kicker mb-2 block text-vb-text">
            {PROFILE_DOB_LABEL}
          </label>
          <Input
            id="profile-dob"
            type="date"
            value={value.dateOfBirth}
            onChange={(e) => onChange("dateOfBirth", e.target.value)}
            required
            max={todayISO()}
            autoComplete="bday"
            disabled={disabled}
            aria-invalid={!!problems.dateOfBirth}
            aria-describedby="profile-dob-hint"
          />
          {problems.dateOfBirth ? (
            <p
              id="profile-dob-hint"
              role="alert"
              className="mt-1.5 border-l-2 border-vb-red pl-3 text-sm text-vb-text"
            >
              {problems.dateOfBirth}
            </p>
          ) : (
            <p id="profile-dob-hint" className="mt-1.5 text-xs text-vb-text-dim">
              {DOB_HINT}
            </p>
          )}
        </div>

        <div>
          <label htmlFor="profile-country" className="f-kicker mb-2 block text-vb-text">
            {PROFILE_COUNTRY_LABEL}
          </label>
          <select
            id="profile-country"
            value={value.country}
            onChange={(e) => onChange("country", e.target.value)}
            required
            disabled={disabled || loadingCountries}
            autoComplete="country"
            aria-invalid={!!problems.country}
            aria-describedby="profile-country-hint"
            className={selectClasses}
          >
            <option value="" disabled>
              {loadingCountries ? "Loading countries" : "Choose a country"}
            </option>
            {countries.map((c) => (
              <option key={c.code} value={c.code}>
                {c.name}
              </option>
            ))}
            <option value={ELSEWHERE}>Somewhere else</option>
          </select>
          {problems.country ? (
            <p
              id="profile-country-hint"
              role="alert"
              className="mt-1.5 border-l-2 border-vb-red pl-3 text-sm text-vb-text"
            >
              {problems.country}
            </p>
          ) : (
            <p id="profile-country-hint" className="mt-1.5 text-xs text-vb-text-dim">
              {COUNTRY_HINT}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
