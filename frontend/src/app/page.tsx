"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { FormaMark } from "@/components/ui/forma-mark";
import { Kicker } from "@/components/ui/kicker";

// FORMA in-app front door. Outcome-led editorial: the relationship,
// not the feature list. ridewithforma.com carries the full pitch.
export default function LandingPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && user) {
      router.push("/dashboard");
    }
  }, [user, loading, router]);

  return (
    <div className="min-h-screen bg-vb-bg">
      <header className="border-b border-vb-border-strong">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-5">
          <FormaMark className="f-display text-xl text-vb-text" />
          <div className="flex items-center gap-2">
            <Link
              href="/login"
              className="f-kicker px-4 py-2 text-vb-text-dim transition-colors hover:text-vb-text"
            >
              Log in
            </Link>
            <Link
              href="/register"
              className="f-press f-kicker rounded-sm bg-vb-red px-5 py-2.5 text-white transition-colors hover:bg-vb-red-dim"
            >
              Get started
            </Link>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6">
        {/* Manifesto hero */}
        <section className="f-rise py-24 md:py-32">
          <Kicker dot flamme>
            A cycling coach with a memory
          </Kicker>
          <h1 className="f-display mt-5 max-w-3xl text-5xl text-vb-text md:text-7xl">
            The plan bends.
            <br />
            The goal{" "}
            <span className="whitespace-nowrap">
              holds
              <span aria-hidden="true" className="f-kite ml-[0.12em]" />
            </span>
          </h1>
          <p className="mt-8 max-w-xl text-lg leading-relaxed text-vb-text-dim">
            Forma reads every ride, remembers every conversation, and uses all
            of it to write what comes next. A season built backwards from your
            race day, and rewritten when your life gets in the way.
          </p>
          <div className="mt-10 flex flex-wrap gap-3">
            <Link
              href="/register"
              className="f-press f-arrow f-kicker inline-flex items-center gap-2 rounded-sm bg-vb-text px-7 py-4 text-white transition-colors hover:bg-vb-red"
            >
              Meet your coach <span className="f-arrow-head">→</span>
            </Link>
            <Link
              href="/login"
              className="f-press f-kicker inline-flex items-center rounded-sm border border-vb-border px-7 py-4 text-vb-text transition-colors hover:border-vb-border-strong"
            >
              Log in
            </Link>
          </div>
        </section>

        {/* The three outcomes */}
        <section className="f-stagger space-y-16 pb-24">
          <div>
            <hr className="f-rule" />
            <div className="grid gap-6 pt-6 md:grid-cols-[120px_1fr]">
              <p className="f-data text-4xl font-semibold text-vb-red">01</p>
              <div>
                <h2 className="f-display text-3xl text-vb-text">
                  It remembers everything.
                </h2>
                <p className="mt-4 max-w-xl leading-relaxed text-vb-text-dim">
                  You mention a wedding in June, once, in passing. When June
                  comes, Forma still knows, and rewrites that week around it
                  with you. The goal date doesn&apos;t move. Every ride, every
                  conversation, the knee, the night shifts: Forma keeps it all,
                  and uses it.
                </p>
              </div>
            </div>
          </div>

          <div>
            <hr className="f-rule" />
            <div className="grid gap-6 pt-6 md:grid-cols-[120px_1fr]">
              <p className="f-data text-4xl font-semibold text-vb-red">02</p>
              <div>
                <h2 className="f-display text-3xl text-vb-text">
                  So your plan is never generic.
                </h2>
                <p className="mt-4 max-w-xl leading-relaxed text-vb-text-dim">
                  Strava tells you what you did. Forma tells you what to do
                  next. Your history, your recovery and your life all shape
                  the next block, so the plan fits you rather than the other
                  way round.
                </p>
              </div>
            </div>
          </div>

          <div>
            <hr className="f-rule" />
            <div className="grid gap-6 pt-6 md:grid-cols-[120px_1fr]">
              <p className="f-data text-4xl font-semibold text-vb-red">03</p>
              <div>
                <h2 className="f-display text-3xl text-vb-text">
                  And it rides with you.
                </h2>
                <p className="mt-4 max-w-xl leading-relaxed text-vb-text-dim">
                  Ride your sessions inside Forma on a Bluetooth smart trainer,
                  from Chrome on a laptop, with live power and a voice in your
                  ear. The ride is saved the moment the cool-down ends.
                </p>
                {/* Carbon instrument strip — a taste of ride mode */}
                <div className="f-carbon mt-6 flex max-w-xl items-center justify-between rounded-sm px-6 py-5">
                  <div>
                    <p className="f-kicker text-vb-chalk-dim">Target</p>
                    <p className="f-data mt-1 text-3xl font-semibold text-vb-chalk">
                      287<span className="text-base text-vb-chalk-dim">w</span>
                    </p>
                  </div>
                  <div>
                    <p className="f-kicker text-vb-chalk-dim">Interval</p>
                    <p className="f-data mt-1 text-3xl font-semibold text-vb-chalk">
                      3<span className="text-base text-vb-chalk-dim">/5</span>
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="f-pulse-dot inline-block h-2 w-2 rounded-full bg-vb-red" />
                    <p className="f-kicker text-vb-red">Live</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* Closing CTA */}
        <section className="f-rise mb-24 bg-vb-text px-8 py-14 text-center md:px-16">
          <p className="f-kicker text-white/60">Invite-only for the founding hundred</p>
          <h2 className="f-display mt-3 text-4xl text-white">
            Got your invite? Start here.
          </h2>
          <Link
            href="/register"
            className="f-press f-arrow f-kicker mt-8 inline-flex items-center gap-2 rounded-sm bg-vb-red px-8 py-4 text-white transition-colors hover:bg-vb-red-bright"
          >
            Get started <span className="f-arrow-head">→</span>
          </Link>
        </section>
      </main>

      <footer className="border-t border-vb-border-subtle">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-8">
          <FormaMark className="f-display text-sm text-vb-text" />
          <p className="f-kicker text-vb-text-muted">ridewithforma.com</p>
        </div>
      </footer>
    </div>
  );
}
