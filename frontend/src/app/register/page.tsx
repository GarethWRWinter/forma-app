"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { authConfig } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { FormaMark } from "@/components/ui/forma-mark";
import { Kicker } from "@/components/ui/kicker";
import { Input } from "@/components/ui/input";
import { Button, Arrow } from "@/components/ui/button";

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
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const { register } = useAuth();
  const router = useRouter();
  const params = useSearchParams();

  // Invite links from the waitlist carry ?invite=FORMA-XXXXXX.
  useEffect(() => {
    const fromLink = params.get("invite");
    if (fromLink) setInviteCode(fromLink.toUpperCase());
  }, [params]);

  const { data: config } = useQuery({
    queryKey: ["auth-config"],
    queryFn: () => authConfig(),
  });
  const inviteRequired = config?.invite_required ?? false;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      await register(email, password, name || undefined, inviteCode || undefined);
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
          {error && (
            <div className="border-l-[3px] border-vb-red bg-vb-surface px-4 py-3 text-sm text-vb-text">
              {error}
            </div>
          )}

          {(inviteRequired || inviteCode) && (
            <div>
              <label className="f-kicker mb-2 block text-vb-text">Invite code</label>
              <Input
                type="text"
                value={inviteCode}
                onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
                required={inviteRequired}
                placeholder="FORMA-XXXXXX"
                className="font-mono uppercase tracking-[0.08em]"
              />
              <p className="mt-1.5 text-xs text-vb-text-dim">
                Forma is invite-only while the Founding Hundred fills. No code
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

          <Button type="submit" variant="flamme" size="lg" disabled={loading} className="w-full">
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
