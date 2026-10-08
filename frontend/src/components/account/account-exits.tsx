"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { billing, users } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Kicker } from "@/components/ui/kicker";
import {
  BILLING_BUTTON,
  BILLING_FAILED,
  BILLING_OPENING,
  DELETE_BUTTON,
  DELETE_CANCEL,
  DELETE_CONFIRM_KICKER,
  DELETE_EMAIL_LABEL,
  DELETE_EXPLAINER,
  DELETE_FAILED,
  DELETE_GO,
  DELETE_WORKING,
  EXITS_KICKER,
  EXPORT_BUTTON,
  EXPORT_DONE,
  EXPORT_FAILED,
  EXPORT_WORKING,
  LOGOUT_BUTTON,
  deleteArmed,
  exitsLead,
  exportFilename,
  offerBilling,
  serverSentence,
  type ExitsMode,
} from "@/lib/accountExits";

/**
 * Manage billing, Download my data, Delete my account and Log out, inside
 * the blocking consent modal. Settings has the same three account actions,
 * but the modal covers Settings, so a rider who won't agree, lives where
 * Forma isn't open, or is held as under 18 would otherwise have no way to
 * cancel, take a copy or leave (re-verification round 3, problem 7). The
 * calls and the confirmation words are Settings' own.
 */
export function AccountExits({
  mode,
  minorHeld,
  disabled,
  className,
}: {
  mode: ExitsMode;
  /** Held as under 18: the portal refuses the account, so it isn't offered. */
  minorHeld: boolean;
  /** While the modal is saving. */
  disabled?: boolean;
  className?: string;
}) {
  const { user, logout } = useAuth();

  // The same cache entry as the membership banner and the Settings card.
  const { data: status, isError: statusFailed } = useQuery({
    queryKey: ["billing-status"],
    queryFn: () => billing.getStatus(),
  });
  const showBilling = offerBilling(status, { loadFailed: statusFailed, minorHeld });

  const [opening, setOpening] = useState(false);
  const [billingError, setBillingError] = useState("");
  const [exportStage, setExportStage] = useState<"idle" | "working" | "done" | "error">("idle");
  const [confirming, setConfirming] = useState(false);
  const [typedEmail, setTypedEmail] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  const busy = !!disabled || deleting;

  const openPortal = async () => {
    setBillingError("");
    setOpening(true);
    try {
      const { url } = await billing.portal();
      window.location.href = url;
    } catch (err) {
      setBillingError(serverSentence(err, BILLING_FAILED));
      setOpening(false);
    }
  };

  const runExport = async () => {
    setExportStage("working");
    try {
      await users.saveMyData(exportFilename());
      setExportStage("done");
    } catch {
      setExportStage("error");
    }
  };

  const deleteAccount = async () => {
    setDeleting(true);
    setDeleteError("");
    try {
      await users.deleteMyAccount();
      logout(); // clears the tokens and sends the rider to /login
    } catch (err) {
      // The server says when it couldn't end the membership; otherwise
      // Settings' own line.
      setDeleteError(serverSentence(err, DELETE_FAILED));
      setDeleting(false);
    }
  };

  return (
    <section aria-labelledby="account-exits-title" className={className}>
      <Kicker className="mb-2">
        <span id="account-exits-title">{EXITS_KICKER}</span>
      </Kicker>
      <p className="text-sm leading-relaxed text-vb-text-dim">{exitsLead(mode, showBilling)}</p>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {showBilling && (
          <Button size="sm" variant="ghost" onClick={openPortal} disabled={busy || opening}>
            {opening ? BILLING_OPENING : BILLING_BUTTON}
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={runExport}
          disabled={busy || exportStage === "working"}
        >
          {exportStage === "working" ? EXPORT_WORKING : EXPORT_BUTTON}
        </Button>
        {!confirming && (
          <Button size="sm" variant="ghost" onClick={() => setConfirming(true)} disabled={busy}>
            {DELETE_BUTTON}
          </Button>
        )}
        <Button size="sm" variant="quiet" onClick={logout} disabled={busy}>
          {LOGOUT_BUTTON}
        </Button>
      </div>

      {exportStage === "done" && (
        <p role="status" className="f-kicker mt-3 text-vb-success">
          {EXPORT_DONE}
        </p>
      )}
      {exportStage === "error" && (
        <p role="alert" className="mt-3 border-l-2 border-vb-red pl-3 text-sm text-vb-text">
          {EXPORT_FAILED}
        </p>
      )}
      {billingError && (
        <p role="alert" className="mt-3 border-l-2 border-vb-red pl-3 text-sm text-vb-text">
          {billingError}
        </p>
      )}

      {confirming && (
        <div className="mt-4 border border-vb-red/40 bg-vb-surface p-4">
          <Kicker flamme>{DELETE_CONFIRM_KICKER}</Kicker>
          <p className="mt-2 text-sm leading-relaxed text-vb-text-dim">{DELETE_EXPLAINER}</p>
          <p className="mt-2 text-sm text-vb-text-dim">
            Type <span className="break-all text-vb-text">{user?.email}</span> below and the
            button goes live.
          </p>
          <div className="mt-3 max-w-xs">
            <label htmlFor="exits-delete-email" className="f-kicker mb-2 block text-vb-text-muted">
              {DELETE_EMAIL_LABEL}
            </label>
            <Input
              id="exits-delete-email"
              type="email"
              autoComplete="off"
              value={typedEmail}
              onChange={(e) => setTypedEmail(e.target.value)}
              disabled={deleting}
            />
          </div>
          {deleteError && (
            <p role="alert" className="mt-3 text-sm text-vb-text-dim">
              {deleteError}
            </p>
          )}
          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="flamme"
              onClick={deleteAccount}
              disabled={!deleteArmed(typedEmail, user?.email) || deleting}
            >
              {deleting ? DELETE_WORKING : DELETE_GO}
            </Button>
            <Button
              size="sm"
              variant="quiet"
              onClick={() => {
                setConfirming(false);
                setTypedEmail("");
                setDeleteError("");
              }}
              disabled={deleting}
            >
              {DELETE_CANCEL}
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
