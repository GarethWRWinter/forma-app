"use client";

import Link from "next/link";
import { useState } from "react";
import { auth } from "@/lib/api";
import { FormaMark } from "@/components/ui/forma-mark";
import { Kicker } from "@/components/ui/kicker";
import { Input } from "@/components/ui/input";
import { Button, Arrow } from "@/components/ui/button";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await auth.forgotPassword(email);
    } catch {
      // Same face either way: this page must not reveal which
      // addresses have accounts.
    } finally {
      setSent(true);
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-start justify-center bg-vb-bg px-6 pt-24">
      <div className="f-rise w-full max-w-md">
        <div className="mb-12 border-b-2 border-vb-border-strong pb-6">
          <h1 className="f-display text-6xl leading-none tracking-[-0.03em]">
            <FormaMark />
          </h1>
        </div>

        <div className="mb-8">
          <Kicker className="mb-2">It happens to everyone</Kicker>
          <h2 className="f-display text-4xl leading-[0.95]">
            Forgotten
            <br />
            password.
          </h2>
        </div>

        {sent ? (
          <div className="space-y-6">
            <div className="border border-vb-border-subtle bg-vb-surface p-5">
              <p className="text-sm leading-relaxed text-vb-text">
                If that address has a Forma account, the reset link is on its
                way. It works for one hour, so check your inbox now, and the
                spam folder if it plays hard to get.
              </p>
            </div>
            <Link href="/login" className="f-kicker text-vb-red hover:text-vb-red-dim">
              ← Back to log in
            </Link>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-6">
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
            <Button type="submit" variant="flamme" size="lg" disabled={loading} className="w-full">
              {loading ? "Sending…" : <>Send the reset link <Arrow /></>}
            </Button>
            <p className="text-sm text-vb-text-dim">
              Remembered it?{" "}
              <Link href="/login" className="f-kicker text-vb-red hover:text-vb-red-dim">
                Log in →
              </Link>
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
