"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { auth } from "@/lib/api";
import { FormaMark } from "@/components/ui/forma-mark";
import { Kicker } from "@/components/ui/kicker";

function VerifyEmailInner() {
  const params = useSearchParams();
  const token = params.get("token") || "";
  const [state, setState] = useState<"working" | "verified" | "failed">(
    token ? "working" : "failed"
  );
  const fired = useRef(false);

  useEffect(() => {
    if (!token || fired.current) return;
    fired.current = true;
    auth
      .verifyEmail(token)
      .then(() => setState("verified"))
      .catch(() => setState("failed"));
  }, [token]);

  return (
    <div className="space-y-6">
      {state === "working" && (
        <p className="text-sm text-vb-text-dim">Checking the link…</p>
      )}
      {state === "verified" && (
        <>
          <div className="border border-vb-border-subtle bg-vb-surface p-5">
            <p className="text-sm leading-relaxed text-vb-text">
              Verified. That address is yours and your coach knows where to
              find you.
            </p>
          </div>
          <Link href="/dashboard" className="f-kicker text-vb-red hover:text-vb-red-dim">
            Into the app →
          </Link>
        </>
      )}
      {state === "failed" && (
        <>
          <div className="border-l-[3px] border-vb-red bg-vb-surface px-4 py-3 text-sm text-vb-text">
            That link has expired or was already used. Request a fresh one
            from inside the app.
          </div>
          <Link href="/login" className="f-kicker text-vb-red hover:text-vb-red-dim">
            Log in →
          </Link>
        </>
      )}
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <div className="flex min-h-screen items-start justify-center bg-vb-bg px-6 pt-24">
      <div className="f-rise w-full max-w-md">
        <div className="mb-12 border-b-2 border-vb-border-strong pb-6">
          <h1 className="f-display text-6xl leading-none tracking-[-0.03em]">
            <FormaMark />
          </h1>
        </div>
        <div className="mb-8">
          <Kicker className="mb-2">One click, done</Kicker>
          <h2 className="f-display text-4xl leading-[0.95]">
            Email
            <br />
            verification.
          </h2>
        </div>
        <Suspense fallback={null}>
          <VerifyEmailInner />
        </Suspense>
      </div>
    </div>
  );
}
