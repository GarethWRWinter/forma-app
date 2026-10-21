"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { legalDocument } from "@/lib/api";
import { FormaMark } from "@/components/ui/forma-mark";

/**
 * The terms or the privacy policy, read from the server word for word: the
 * same published file the consent rows are stamped with, so this page can
 * never show different words from the ones a rider agreed to.
 */
export function LegalPage({ doc }: { doc: "terms" | "privacy" }) {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["legal", doc],
    queryFn: () => legalDocument(doc),
    staleTime: Infinity,
    retry: 1,
  });

  return (
    <div className="min-h-screen bg-vb-bg px-6 pb-24 pt-16">
      <div className="mx-auto w-full max-w-2xl">
        <div className="mb-10 border-b-2 border-vb-border-strong pb-6">
          <Link href="/" aria-label="Forma home" className="f-display text-4xl leading-none">
            <FormaMark />
          </Link>
        </div>
        {isLoading ? (
          <p className="text-sm text-vb-text-dim">Loading…</p>
        ) : isError || !data ? (
          <p className="text-sm text-vb-text">
            This page didn&apos;t load.{" "}
            <button
              type="button"
              onClick={() => refetch()}
              className="underline underline-offset-2 hover:text-vb-red"
            >
              Try again
            </button>
          </p>
        ) : (
          <article className="prose prose-sm max-w-none text-vb-text prose-headings:text-vb-text prose-p:text-vb-text prose-li:text-vb-text prose-strong:text-vb-text prose-a:text-vb-red">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.text}</ReactMarkdown>
            <p className="f-kicker mt-10 text-vb-text-muted">Version {data.doc_version}</p>
          </article>
        )}
      </div>
    </div>
  );
}
