"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { auth } from "@/lib/api";
import { FormaMark } from "@/components/ui/forma-mark";
import { Kicker } from "@/components/ui/kicker";
import { Input } from "@/components/ui/input";
import { Button, Arrow } from "@/components/ui/button";

function ResetPasswordForm() {
  const params = useSearchParams();
  const router = useRouter();
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("Those two don't match. Type them again, slowly.");
      return;
    }
    setLoading(true);
    try {
      await auth.resetPassword(token, password);
      setDone(true);
      setTimeout(() => router.push("/login"), 2500);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "That link has expired. Request a new one.");
    } finally {
      setLoading(false);
    }
  };

  if (!token) {
    return (
      <div className="space-y-6">
        <div className="border-l-[3px] border-vb-red bg-vb-surface px-4 py-3 text-sm text-vb-text">
          This page needs the link from your email. Open the email and click
          the link directly, or request a fresh one.
        </div>
        <Link href="/forgot-password" className="f-kicker text-vb-red hover:text-vb-red-dim">
          Request a reset link →
        </Link>
      </div>
    );
  }

  if (done) {
    return (
      <div className="border border-vb-border-subtle bg-vb-surface p-5">
        <p className="text-sm leading-relaxed text-vb-text">
          Done. New password set, every old session signed out. Taking you to
          the log in…
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {error && (
        <div className="border-l-[3px] border-vb-red bg-vb-surface px-4 py-3 text-sm text-vb-text">
          {error}
        </div>
      )}
      <div>
        <label className="f-kicker mb-2 block text-vb-text">New password</label>
        <Input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={8}
          autoComplete="new-password"
          placeholder="At least 8 characters"
        />
      </div>
      <div>
        <label className="f-kicker mb-2 block text-vb-text">Type it again</label>
        <Input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
          minLength={8}
          autoComplete="new-password"
          placeholder="Same again"
        />
      </div>
      <Button type="submit" variant="flamme" size="lg" disabled={loading} className="w-full">
        {loading ? "Setting…" : <>Set the new password <Arrow /></>}
      </Button>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <div className="flex min-h-screen items-start justify-center bg-vb-bg px-6 pt-24">
      <div className="f-rise w-full max-w-md">
        <div className="mb-12 border-b-2 border-vb-border-strong pb-6">
          <h1 className="f-display text-6xl leading-none tracking-[-0.03em]">
            <FormaMark />
          </h1>
        </div>
        <div className="mb-8">
          <Kicker className="mb-2">Fresh start</Kicker>
          <h2 className="f-display text-4xl leading-[0.95]">
            New
            <br />
            password.
          </h2>
        </div>
        <Suspense fallback={null}>
          <ResetPasswordForm />
        </Suspense>
      </div>
    </div>
  );
}
