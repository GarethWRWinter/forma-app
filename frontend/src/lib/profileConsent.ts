/**
 * Date of birth and where they live, for an account that never gave them.
 * The beta riders joined before either was asked, so they agreed to terms
 * that say Forma is for adults in the UK without anyone checking.
 * When /users/me says needs_profile_consent, the re-acceptance modal asks for
 * both alongside the terms box, auth.reacceptTerms sends them, and the server
 * runs the same age and country checks as sign-up and records the age row.
 *
 * The server decides who is 18 and where Forma is open; this only checks
 * that both were filled in and puts each refusal under the field it is about.
 *
 * No runtime imports: the tests run this file straight under Node.
 * Tests: node --test src/lib/profile-consent.test.mjs
 */

import type { ProfileConsentInput, UserProfile } from "@/lib/api";

/** True only when the server says so. An older server that doesn't send the
    flag asks for nothing new. */
export function profileConsentDue(
  user: Pick<UserProfile, "needs_profile_consent"> | null | undefined
): boolean {
  return user?.needs_profile_consent === true;
}

/** Above the two fields in the modal. */
export const PROFILE_CONSENT_INTRO =
  "Forma didn't ask for your date of birth or where you live when you joined. It needs both now: Forma is for adults, 18 and over, and where you live sets the emergency numbers the coach gives you.";

export const PROFILE_DOB_LABEL = "Date of birth";
export const PROFILE_COUNTRY_LABEL = "Where you live";
export const PROFILE_NO_DOB = "Add your date of birth.";
export const PROFILE_NO_COUNTRY = "Choose where you live.";
/** "Somewhere else" can't be sent: the server only takes a real country code.
    Said here instead, in the words the server uses for a country it doesn't
    serve, then pointed at the ways out under the form (Manage billing,
    Download my data, Delete my account, Log out), so it is never a dead end.
    The same sentence as accountExits.countryClosedText("elsewhere"). */
export const PROFILE_ELSEWHERE =
  "Forma isn't available in your country yet, so this account can't carry on for now. What you can do instead is below.";

export interface ProfileChoices {
  dateOfBirth: string;
  country: string;
}

export type ProfileField = "dateOfBirth" | "country";
export type ProfileProblems = Partial<Record<ProfileField, string>>;

/** A real calendar day as YYYY-MM-DD, no later than today. The date picker
    hands back "" for a half-typed date, so that counts as missing. */
function isRealPastDate(value: string, today: Date): boolean {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!m) return false;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const day = new Date(y, mo - 1, d);
  if (day.getFullYear() !== y || day.getMonth() !== mo - 1 || day.getDate() !== d) {
    return false;
  }
  const end = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return day.getTime() <= end.getTime();
}

/** What stops the modal sending, field by field. Empty when it can send.
    Age and country rules are the server's; only "Somewhere else" is caught
    here, because it has no code to send. */
export function profileProblems(
  input: ProfileChoices,
  elsewhere: string = "elsewhere",
  today: Date = new Date()
): ProfileProblems {
  const problems: ProfileProblems = {};
  if (!isRealPastDate(input.dateOfBirth || "", today)) problems.dateOfBirth = PROFILE_NO_DOB;
  const country = (input.country || "").trim();
  if (!country) problems.country = PROFILE_NO_COUNTRY;
  else if (country === elsewhere) problems.country = PROFILE_ELSEWHERE;
  return problems;
}

/** What reacceptTerms carries, or undefined when the modal didn't ask. */
export function profilePayload(
  due: boolean,
  input: ProfileChoices
): ProfileConsentInput | undefined {
  if (!due) return undefined;
  return { dateOfBirth: input.dateOfBirth.trim(), country: input.country.trim().toUpperCase() };
}

/**
 * Which field a refusal from POST /auth/reaccept-terms is about, so it shows
 * under that field rather than under the terms box: the under-18 and bad-date
 * sentences under the date of birth, "isn't available in your country" and
 * "pick where you live" under the country. FastAPI's own 422 arrives as
 * "date_of_birth: ..." or "country: ..." (normalizeErrorMessage). Null for
 * anything else, which stays under the terms box.
 */
export function profileErrorField(message: string | null | undefined): ProfileField | null {
  const text = (message || "").trim();
  if (!text) return null;
  if (/^date_of_birth\b/i.test(text)) return "dateOfBirth";
  if (/^country\b/i.test(text)) return "country";
  if (/date of birth|\b18\b|under 18|\badults?\b/i.test(text)) return "dateOfBirth";
  if (/where you live|available in|\bcountry\b|emergency numbers/i.test(text)) return "country";
  return null;
}
