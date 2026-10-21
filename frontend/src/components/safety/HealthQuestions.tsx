"use client";

import type { ScreeningQuestion, ScreeningResult } from "@/lib/api";
import { cn } from "@/lib/utils";
import { CoachNote } from "@/components/ui/coach-note";
import {
  LONG_BREAK_QUESTION,
  SCREENING_QUESTIONS,
  type ScreeningAnswers,
} from "@/components/safety/safety-rules";

/** One question, answered with an explicit yes or no. Nothing is
    preselected: a health answer has to be the rider's own. */
export function YesNo({
  question,
  value,
  onChange,
  number,
}: {
  question: string;
  value: boolean | undefined;
  onChange: (value: boolean) => void;
  number?: number;
}) {
  const option = (selected: boolean) =>
    cn(
      "f-press min-w-[64px] rounded-sm border px-3 py-2 text-xs font-medium transition-colors",
      selected
        ? "border-vb-red bg-vb-surface text-vb-text"
        : "border-vb-border-subtle bg-vb-surface text-vb-text-dim hover:border-vb-border"
    );
  return (
    <div className="flex flex-col gap-3 border-b border-vb-border-subtle py-4 last:border-b-0 sm:flex-row sm:items-start sm:justify-between">
      <p className="text-sm leading-relaxed text-vb-text sm:pr-4">
        {number != null && (
          <span className="f-data mr-2 text-xs text-vb-text-muted">
            {String(number).padStart(2, "0")}
          </span>
        )}
        {question}
      </p>
      <div className="flex shrink-0 gap-2" role="radiogroup" aria-label={question}>
        <button
          type="button"
          role="radio"
          aria-checked={value === true}
          onClick={() => onChange(true)}
          className={option(value === true)}
        >
          Yes
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={value === false}
          onClick={() => onChange(false)}
          className={option(value === false)}
        >
          No
        </button>
      </div>
    </div>
  );
}

/** The eight screening questions, in order. Each answer is reported on its
    own, so the parent can fold it in with a functional update. */
export function HealthQuestions({
  answers,
  onAnswer,
  className,
}: {
  answers: ScreeningAnswers;
  onAnswer: (id: ScreeningQuestion, value: boolean) => void;
  className?: string;
}) {
  return (
    <div className={className}>
      {SCREENING_QUESTIONS.map((q, i) => (
        <YesNo
          key={q.id}
          number={i + 1}
          question={q.text}
          value={answers[q.id]}
          onChange={(v) => onAnswer(q.id, v)}
        />
      ))}
    </div>
  );
}

/** The training question that feeds the layoff gate. Not health data. */
export function LongBreakQuestion({
  value,
  onChange,
}: {
  value: boolean | undefined;
  onChange: (value: boolean) => void;
}) {
  return <YesNo question={LONG_BREAK_QUESTION} value={value} onChange={onChange} />;
}

/** What the answers mean, in the server's words: the tier message, then the
    extra lines for medicine, injury or pregnancy. */
export function ScreeningResultNote({
  result,
  coachName,
  className,
}: {
  result: ScreeningResult;
  coachName?: string;
  className?: string;
}) {
  const kicker =
    result.tier === "hold_all"
      ? "On hold for now"
      : result.tier === "easy_only"
        ? "Easy riding first"
        : "Noted";
  return (
    <CoachNote kicker={kicker} coachName={coachName || "Forma"} className={className}>
      <p>{result.message}</p>
      {result.extra_lines.map((line) => (
        <p key={line} className="mt-3">
          {line}
        </p>
      ))}
    </CoachNote>
  );
}
