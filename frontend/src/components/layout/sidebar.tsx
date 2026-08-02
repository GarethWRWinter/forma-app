"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { LogOut, Menu, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuth } from "@/lib/auth-context";
import { FormaMark } from "@/components/ui/forma-mark";

/**
 * FORMA sidebar, a bold masthead, mono nav, one flamme dot marking
 * where you are. On mobile the top bar and drawer invert to carbon.
 */

// Brand v2 IA — the five words: TODAY · COACH · RIDES · FORM · PLAN,
// then the deeper rooms (Brain, Palmarès, Settings). Goals live INSIDE
// the plan (the goal is the plan's masthead, not a sibling page).
const navItems = [
  { href: "/dashboard", label: "Today" },
  { href: "/dashboard/coach", label: "Coach" },
  { href: "/dashboard/rides", label: "Rides" },
  { href: "/dashboard/performance", label: "Form" },
  { href: "/dashboard/training", label: "Plan" },
  { href: "/dashboard/brain", label: "Brain" },
  { href: "/dashboard/palmares", label: "Palmarès" },
  { href: "/dashboard/settings", label: "Settings" },
];

export function Sidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    setMobileOpen(false);
  }, [pathname]);

  useEffect(() => {
    document.body.style.overflow = mobileOpen ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [mobileOpen]);

  const sidebarContent = (
    <>
      {/* Masthead */}
      <div className="px-7 pt-10 pb-2">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="f-display text-[22px] leading-none text-vb-chalk md:text-vb-text">
              <FormaMark />
            </h1>
            <p className="f-kicker mt-2.5 text-[10px] text-vb-chalk-dim md:text-vb-text-muted">
              Your coach · A memory for everything, an eye on race day
            </p>
          </div>
          <button
            onClick={() => setMobileOpen(false)}
            className="text-vb-chalk-dim hover:text-vb-chalk md:hidden"
            aria-label="Close menu"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-5 py-8">
        <ul className="space-y-1">
          {navItems.map((item) => {
            const isActive =
              pathname === item.href ||
              (item.href !== "/dashboard" && pathname.startsWith(item.href)) ||
              // Goals live inside the plan since the nav consolidation.
              (item.href === "/dashboard/training" &&
                pathname.startsWith("/dashboard/goals"));

            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={cn(
                    "f-kicker flex items-center gap-3.5 rounded-sm px-3 py-2.5 transition-colors",
                    isActive
                      ? "text-vb-chalk md:text-vb-text"
                      : "text-vb-chalk-dim hover:text-vb-chalk md:text-vb-text-muted md:hover:text-vb-text"
                  )}
                >
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full transition-colors",
                      isActive ? "bg-vb-red" : "bg-transparent"
                    )}
                  />
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* User footer */}
      <div className="mx-5 mb-6 flex items-center justify-between gap-3 border-t border-vb-carbon-raised pt-5 md:border-vb-border-subtle">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-vb-carbon-raised font-display text-sm font-semibold text-vb-chalk md:bg-vb-text md:text-white">
            {(user?.full_name?.[0] || "R").toUpperCase()}
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-medium leading-none text-vb-chalk md:text-vb-text">
              {user?.full_name?.split(" ")[0] || "Rider"}
            </p>
            {user?.ftp && (
              <p className="f-data mt-1 text-xs text-vb-chalk-dim md:text-vb-text-muted">
                FTP ·{" "}
                <span className="text-vb-chalk md:text-vb-text-dim">
                  {user.ftp}w
                </span>
              </p>
            )}
          </div>
        </div>
        <button
          onClick={logout}
          className="rounded-sm border border-vb-carbon-raised p-2 text-vb-chalk-dim transition-colors hover:border-vb-chalk-dim hover:text-vb-chalk md:border-vb-border-subtle md:text-vb-text-muted md:hover:border-vb-border md:hover:text-vb-text"
          title="Log out"
          aria-label="Log out"
        >
          <LogOut className="h-4 w-4" />
        </button>
      </div>
    </>
  );

  return (
    <>
      {/* Mobile header bar, carbon-inverted */}
      <div className="fixed inset-x-0 top-0 z-40 flex h-14 items-center gap-4 border-b border-vb-carbon-raised bg-vb-carbon px-4 text-vb-chalk md:hidden">
        <button
          onClick={() => setMobileOpen(true)}
          className="text-vb-chalk"
          aria-label="Open menu"
        >
          <Menu className="h-6 w-6" />
        </button>
        <FormaMark className="f-display text-lg text-vb-chalk" />
      </div>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-50 bg-vb-text/30 backdrop-blur-sm md:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Sidebar panel, carbon drawer on mobile, paper column on desktop */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-72 flex-col bg-vb-carbon text-vb-chalk transition-transform duration-300 ease-in-out md:static md:z-auto md:w-64 md:translate-x-0 md:border-r md:border-vb-border-subtle md:bg-vb-bg md:text-vb-text",
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        {sidebarContent}
      </aside>
    </>
  );
}
