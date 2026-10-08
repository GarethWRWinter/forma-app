"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { authConfig } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { FormaMark } from "@/components/ui/forma-mark";
import { Kicker } from "@/components/ui/kicker";
import { Input } from "@/components/ui/input";
import { Button, Arrow } from "@/components/ui/button";
import { ConsentBox } from "@/components/safety/ConsentBox";
import {
  BLOCKED_COUNTRIES,
  COUNTRIES,
  COUNTRY_HINT,
  DOB_HINT,
  ELSEWHERE,
  ELSEWHERE_BLOCKED,
  HEALTH_BOX,
  SAFETY_PANEL_TEXT,
  SAFETY_PANEL_TITLE,
  TERMS_BOX,
  UNDER_18,
  US_CA_BLOCKED,
  ageOn,
  registrationProblems,
  type RegistrationProblems,
} from "@/components/safety/safety-rules";
import {
  allowedCountriesFrom,
  countryBlockFor,
  countryOptions,
} from "@/lib/safetyPrompts";

// The select shares the kit Input look (Input covers <input> only).
const selectClasses =
  "flex h-11 w-full rounded-sm border border-vb-border bg-vb-surface px-3 py-2 text-sm text-vb-text focus-visible:border-vb-red focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-vb-red";

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

/**
 * "Leave your email and I'll tell you when it is." The public waitlist takes
 * it. Country rides along and is stored, so these riders can be told apart
 * from the founding queue.
 */
async function joinWaitlist(email: string, name: string, country: string): Promise<void> {
  const res = await fetch("/api/v1/waitlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email,
      name: name || undefined,
      country: country === ELSEWHERE ? undefined : country,
    }),
  });
  if (!res.ok) throw new Error("That didn't go through. Try again in a minute.");
}

export default function RegisterPage() {
  return (
    <Suspense fallback={null}>
      <RegisterInner />
    </Suspense>
  );
}

function RegisterInner() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [dateOfBirth, setDateOfBirth] = useState("");
  const [country, setCountry] = useState("");
  // Two separate boxes, both unticked: agreeing to the terms never doubles
  // as consent to health data (UK GDPR Art 7(2)).
  const [termsAccepted, setTermsAccepted] = useState(false);
  const [healthConsent, setHealthConsent] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [problems, setProblems] = useState<RegistrationProblems>({});
  const [loading, setLoading] = useState(false);
  const [waitlist, setWaitlist] = useState<"idle" | "sending" | "done">("idle");
  const [waitlistError, setWaitlistError] = useState("");
  const { register } = useAuth();
  const router = useRouter();
  const params = useSearchParams();

  // Invite links from the waitlist carry ?invite=FORMA-XXXXXX.
  useEffect(() => {
    const fromLink = params.get("invite");
    if (fromLink) setInviteCode(fromLink.toUpperCase());
  }, [params]);

  const { data: config, isPending: configPending } = useQuery({
    queryKey: ["auth-config"],
    queryFn: () => authConfig(),
  });
  const inviteRequired = config?.invite_required ?? false;

  // Where riders can join from is the server's list (GET /auth/config), the
  // same one it enforces on sign-up. Only if it sends none does the form fall
  // back to its own; the server checks again either way.
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
  const blockKind = countryBlockFor(country, allowedCodes, BLOCKED_COUNTRIES, ELSEWHERE);
  const countryBlocked =
    blockKind === "us_ca" ? US_CA_BLOCKED : blockKind === "elsewhere" ? ELSEWHERE_BLOCKED : null;

  // Said the moment the date or the country is picked, not only on submit,
  // and the button stays off: an under-18 or a rider in Ohio is never sent.
  const age = ageOn(dateOfBirth);
  const dobProblem = age !== null && age < 18 ? UNDER_18 : undefined;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    const found = registrationProblems({ dateOfBirth, country, termsAccepted, healthConsent });
    // The country is checked against the server's list, not a fixed one.
    if (country) {
      if (countryBlocked) found.country = countryBlocked;
      else delete found.country;
    }
    setProblems(found);
    if (Object.keys(found).length > 0) return;
    setLoading(true);

    try {
      await register({
        email,
        password,
        fullName: name || undefined,
        inviteCode: inviteCode || undefined,
        dateOfBirth,
        country,
        termsAccepted,
        healthConsent,
        termsTextShown: TERMS_BOX.text,
        healthTextShown: HEALTH_BOX.text,
      });
      router.push("/onboarding");
    } catch (err: unknown) {
      const msg =
        err instanceof Error
          ? err.message
          : "That didn't go through. Check the details and try again.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-start justify-center bg-vb-bg px-6 pt-24">
      <div className="f-rise w-full max-w-md">
        {/* Masthead */}
        <div className="mb-12 border-b-2 border-vb-border-strong pb-6">
          <h1 className="f-display text-6xl leading-none tracking-[-0.03em]">
            <FormaMark />
          </h1>
          <Kicker className="mt-3">Your coach · A memory for everything, an eye on race day</Kicker>
        </div>

        {/* Header */}
        <div className="mb-8">
          <Kicker dot flamme className="mb-2">
            Get started
          </Kicker>
          <h2 className="f-display text-4xl leading-[0.95]">
            Meet the
            <br />
            coach.
          </h2>
          <p className="mt-4 max-w-sm text-sm leading-relaxed text-vb-text-dim">
            A coach who remembers everything and builds your season around
            the life you actually live. Two minutes from now, Forma starts
            learning how you ride.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-6">
          {(inviteRequired || inviteCode) && (
            <div>
              <label className="f-kicker mb-2 block text-vb-text">Invite code</label>
              <Input
                type="text"
                value={inviteCode}
                onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
                required={inviteRequired}
                placeholder="From your invite email"
                className="font-mono uppercase tracking-[0.08em]"
              />
              <p className="mt-1.5 text-xs text-vb-text-dim">
                Forma is invite-only while the founding hundred fills. No code
                yet? Join the list at ridewithforma.com.
              </p>
            </div>
          )}

          <div>
            <label className="f-kicker mb-2 block text-vb-text">Full name</label>
            <Input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
              placeholder="Alex Rivera"
            />
          </div>

          <div>
            <label className="f-kicker mb-2 block text-vb-text">Email</label>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
              placeholder="you@example.com"
            />
          </div>

          <div>
            <label className="f-kicker mb-2 block text-vb-text">Password</label>
            <div className="relative">
              <Input
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                autoComplete="new-password"
                placeholder="At least 8 characters"
                className="pr-11"
              />
              <button
                type="button"
                onClick={() => setShowPassword((s) => !s)}
                aria-label={showPassword ? "Hide password" : "Show password"}
                className="absolute inset-y-0 right-0 flex w-11 items-center justify-center text-vb-text-dim hover:text-vb-text"
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" />
                ) : (
                  <Eye className="h-4 w-4" />
                )}
              </button>
            </div>
          </div>

          <div>
            <label htmlFor="dob" className="f-kicker mb-2 block text-vb-text">
              Date of birth
            </label>
            <Input
              id="dob"
              type="date"
              value={dateOfBirth}
              onChange={(e) => {
                setDateOfBirth(e.target.value);
                setProblems((p) => ({ ...p, dateOfBirth: undefined }));
              }}
              required
              max={todayISO()}
              autoComplete="bday"
              aria-describedby="dob-hint"
            />
            {dobProblem || problems.dateOfBirth ? (
              <p id="dob-hint" className="mt-1.5 border-l-2 border-vb-red pl-3 text-sm text-vb-text">
                {dobProblem || problems.dateOfBirth}
              </p>
            ) : (
              <p id="dob-hint" className="mt-1.5 text-xs text-vb-text-dim">
                {DOB_HINT}
              </p>
            )}
          </div>

          <div>
            <label htmlFor="country" className="f-kicker mb-2 block text-vb-text">
              Where you live
            </label>
            <select
              id="country"
              value={country}
              onChange={(e) => {
                setCountry(e.target.value);
                setProblems((p) => ({ ...p, country: undefined }));
                setWaitlist("idle");
                setWaitlistError("");
              }}
              required
              disabled={configPending}
              autoComplete="country"
              aria-describedby="country-hint"
              className={selectClasses}
            >
              <option value="" disabled>
                {configPending ? "Loading countries" : "Choose a country"}
              </option>
              {countries.map((c) => (
                <option key={c.code} value={c.code}>
                  {c.name}
                </option>
              ))}
              <option value={ELSEWHERE}>Somewhere else</option>
            </select>
            {countryBlocked ? (
              <div id="country-hint" className="mt-2 border-l-2 border-vb-red pl-3">
                <p className="text-sm leading-relaxed text-vb-text">{countryBlocked}</p>
                {waitlist === "done" ? (
                  <p className="mt-2 text-sm text-vb-text-dim">
                    Thanks. I&apos;ll email you at {email}.
                  </p>
                ) : (
                  <button
                    type="button"
                    disabled={waitlist === "sending"}
                    onClick={async () => {
                      setWaitlistError("");
                      if (!/^\S+@\S+\.\S+$/.test(email.trim())) {
                        setWaitlistError("Add your email above first.");
                        return;
                      }
                      setWaitlist("sending");
                      try {
                        await joinWaitlist(email.trim(), name.trim(), country);
                        setWaitlist("done");
                      } catch (err) {
                        setWaitlist("idle");
                        setWaitlistError(
                          err instanceof Error ? err.message : "That didn't go through. Try again."
                        );
                      }
                    }}
                    className="f-kicker mt-2 text-vb-red transition-colors hover:text-vb-red-dim disabled:opacity-50"
                  >
                    {waitlist === "sending" ? "Sending" : "Tell me when it opens →"}
                  </button>
                )}
                {waitlistError && (
                  <p className="mt-1.5 text-sm text-vb-text-dim">{waitlistError}</p>
                )}
              </div>
            ) : problems.country ? (
              <p id="country-hint" className="mt-1.5 border-l-2 border-vb-red pl-3 text-sm text-vb-text">
                {problems.country}
              </p>
            ) : (
              <p id="country-hint" className="mt-1.5 text-xs text-vb-text-dim">
                {COUNTRY_HINT}
              </p>
            )}
          </div>

          {/* Always visible, above the boxes, never behind a link. */}
          <section
            aria-labelledby="before-you-join"
            className="border border-vb-border-subtle border-l-[3px] border-l-vb-red bg-vb-surface px-5 py-4"
          >
            <h3 id="before-you-join" className="f-kicker text-vb-red">
              {SAFETY_PANEL_TITLE}
            </h3>
            <p className="mt-2 text-sm leading-relaxed text-vb-text">{SAFETY_PANEL_TEXT}</p>
          </section>

          <ConsentBox
            label={TERMS_BOX}
            checked={termsAccepted}
            onChange={(v) => {
              setTermsAccepted(v);
              if (v) setProblems((p) => ({ ...p, terms: undefined }));
            }}
            error={problems.terms}
          />

          <ConsentBox
            label={HEALTH_BOX}
            checked={healthConsent}
            onChange={(v) => {
              setHealthConsent(v);
              if (v) setProblems((p) => ({ ...p, health: undefined }));
            }}
            error={problems.health}
          />

          {error && (
            <div
              role="alert"
              className="border-l-[3px] border-vb-red bg-vb-surface px-4 py-3 text-sm text-vb-text"
            >
              {error}
            </div>
          )}

          <Button
            type="submit"
            variant="flamme"
            size="lg"
            disabled={loading || !!countryBlocked || !!dobProblem}
            className="w-full"
          >
            {loading ? "Creating account…" : <>Create account <Arrow /></>}
          </Button>
        </form>

        <p className="mt-10 border-t border-vb-border-subtle pt-6 text-sm text-vb-text-dim">
          Already have an account?{" "}
          <Link href="/login" className="f-kicker text-vb-red hover:text-vb-red-dim">
            Log in →
          </Link>
        </p>
      </div>
    </div>
  );
}
