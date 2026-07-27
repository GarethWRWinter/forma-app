"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { useAuth } from "@/lib/auth-context";
import { FormaMark } from "@/components/ui/forma-mark";
import { Kicker } from "@/components/ui/kicker";
import { Input } from "@/components/ui/input";
import { Button, Arrow } from "@/components/ui/button";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [rememberMe, setRememberMe] = useState(true);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      await login(email, password, rememberMe);
      router.push("/dashboard");
    } catch (err: unknown) {
      const msg =
        err instanceof Error ? err.message : "That email and password don't match. Try again.";
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
          <Kicker className="mb-2">Welcome back</Kicker>
          <h2 className="f-display text-4xl leading-[0.95]">
            Log in to
            <br />
            your coach.
          </h2>
        </div>

        <form onSubmit={handleSubmit} className="space-y-6">
          {error && (
            <div className="border-l-[3px] border-vb-red bg-vb-surface px-4 py-3 text-sm text-vb-text">
              {error}
            </div>
          )}

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
                autoComplete="current-password"
                placeholder="••••••••"
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

          <label className="flex cursor-pointer select-none items-center gap-2.5">
            <button
              type="button"
              role="checkbox"
              aria-checked={rememberMe}
              onClick={() => setRememberMe((v) => !v)}
              className={
                "flex h-4 w-4 shrink-0 items-center justify-center border transition-colors " +
                (rememberMe ? "border-vb-red bg-vb-red" : "border-vb-border bg-vb-surface")
              }
            >
              {rememberMe && (
                <span className="text-[10px] leading-none text-white">✓</span>
              )}
            </button>
            <span
              className="f-kicker text-vb-text-dim"
              onClick={() => setRememberMe((v) => !v)}
            >
              Keep me logged in for 30 days
            </span>
          </label>

          <Button type="submit" variant="flamme" size="lg" disabled={loading} className="w-full">
            {loading ? "Logging in…" : <>Log in <Arrow /></>}
          </Button>
        </form>

        <p className="mt-10 border-t border-vb-border-subtle pt-6 text-sm text-vb-text-dim">
          New here?{" "}
          <Link
            href="/register"
            className="f-kicker text-vb-red hover:text-vb-red-dim"
          >
            Meet your coach →
          </Link>
        </p>
      </div>
    </div>
  );
}
