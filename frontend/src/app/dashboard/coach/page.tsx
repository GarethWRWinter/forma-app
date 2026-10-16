"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import React, { useState, useRef, useEffect, useCallback, useMemo, Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  Send,
  Plus,
  MessageCircle,
  Bot,
  User,
  Mic,
  MoreHorizontal,
  Pin,
  Star,
  Archive,
  ArchiveRestore,
  Pencil,
  Trash2,
  Volume2,
  VolumeX,
  Paperclip,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ApiError,
  chat,
  goals as goalsApi,
  type ChatAttachmentCard,
  type ChatSession,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { CoachDot, CoachGlyph } from "@/components/ui/coach-glyph";
import { CadenceSpinner } from "@/components/ui/cadence-spinner";
import { StarterChips, useCoachStarters } from "@/components/coach/coach-starters";
import {
  AttachButton,
  AttachmentChips,
  attachmentOnlyMessage,
  DATA_IN_HREF,
  isZip,
  MAX_ATTACHMENTS,
  type ComposerAttachment,
} from "@/components/coach/ride-attachments";
import { FileCard, refreshRideData } from "@/components/coach/file-card";
import { useVoiceChat } from "@/hooks/useVoiceChat";
import { useAuth } from "@/lib/auth-context";
import { VoiceButton } from "@/components/voice/VoiceButton";
import { VoiceIndicator } from "@/components/voice/VoiceIndicator";

/** What the coach says when a message fails. "Send that again" is only the
    right answer when the connection dropped: a rider without a membership
    (402) or over the month's limit (429) would otherwise resend forever and
    never learn why (launch audit, 4 Oct 2026). */
function sendFailureMessage(error: unknown): string {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 401) {
    return error.message;
  }
  return "I lost the connection there. Send that again.";
}

/** When a chat last moved: its newest message, or its opening while empty. */
const lastActive = (s: ChatSession) =>
  s.last_message_at || s.updated_at || s.created_at || "";

/** A sidebar label in plain words. Titles named before the server cleaned
    them can still carry a markdown heading or a dash (16 Oct 2026: "# Sa
    Calobra Sub-30 Preparati..."), so the list cleans them on the way in. */
function chatTitle(title: string | null | undefined, fallback = "Chat"): string {
  const clean = (title || "")
    .replace(/^Chat - /, "Chat · ")
    .replace(/^\s*#{1,6}\s+/, "")
    .replace(/\*+|__|`/g, "")
    .replace(/(\d)\s*[\u2013\u2014]\s*(?=\d)/g, "$1-")
    .replace(/\s*[\u2013\u2014]\s*/g, ", ")
    .replace(/\s{2,}/g, " ")
    .replace(/^[\s,]+|[\s,]+$/g, "");
  return clean || fallback;
}

/** "1 message", "12 messages"; an empty chat says so. */
function messageCount(n: number): string {
  if (n === 0) return "No messages yet";
  return n === 1 ? "1 message" : `${n} messages`;
}

function CoachPageInner() {
  const { user: authUser } = useAuth();
  const coach = authUser?.coach_name || "Forma";
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const goalId = searchParams.get("goal_id");

  const isDebrief = searchParams.get("debrief") === "true";

  // Everything a coach tool can change: sessions, the plan itself (a goal
  // filed in chat can rebuild it) and the goals.
  const refreshPlan = () => {
    for (const key of [
      "workouts-week",
      "today-workouts",
      "today-workout-detail",
      "plans",
      "plan-detail",
      "goals",
    ]) {
      queryClient.invalidateQueries({ queryKey: [key] });
    }
  };

  // Handoffs from briefings and other surfaces arrive as ?ask=... and
  // prefill the input; the rider presses send, nothing is sent for them.
  const askParam = searchParams.get("ask");
  useEffect(() => {
    if (askParam) setInput(askParam);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [askParam]);

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<
    {
      role: "user" | "assistant";
      content: string;
      id?: string;
      created_at?: string;
      /** filenames handed over with this message, so the thread shows the swap */
      attachments?: string[];
      /** the same files' ids, in the same order, so each card sits under it */
      attachmentIds?: string[];
    }[]
  >([]);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const resumedRef = useRef(false);
  const [input, setInput] = useState("");
  // What the coach is doing right now (tool work), shown while it runs.
  const [status, setStatus] = useState<string | null>(null);
  // The last few turns steer which starter chips lead: the conversation's
  // own topics outrank the general pool.
  const recentText = useMemo(
    () => messages.slice(-6).map((m) => m.content).join(" "),
    [messages]
  );
  const starters = useCoachStarters(recentText);
  const [streaming, setStreaming] = useState(false);
  const [goalMessagePrefilled, setGoalMessagePrefilled] = useState(false);
  const [voiceMode, setVoiceMode] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const audioMutedRef = useRef(false);

  // Ride files waiting on the composer, and whatever went wrong last time.
  // dataIn adds the way to Settings, Data in, where a batch of rides belongs.
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [attachError, setAttachError] = useState<{
    text: string;
    dataIn?: boolean;
  } | null>(null);
  const attachKeyRef = useRef(0);
  // The voice path fires from a callback the hook holds, so it reads the
  // chips through a ref rather than a closure that may be a turn behind.
  const attachmentsRef = useRef<ComposerAttachment[]>([]);
  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  const uploading = attachments.some((a) => a.status === "uploading");
  const readyAttachments = attachments.filter(
    (a) => a.status === "ready" && a.id
  );

  // The file cards in the thread, by attachment id. Each one sits under the
  // message that shared its file, and comes back with the session on reload.
  const [cards, setCards] = useState<Record<string, ChatAttachmentCard>>({});
  // When the rider last chose on each card. A read that left the server
  // before that choice landed is behind it, and must not undo it on screen.
  const choseAtRef = useRef<Record<string, number>>({});
  const mergeCards = useCallback(
    (incoming: ChatAttachmentCard[] | undefined, readAt: number) => {
      if (!incoming?.length) return;
      setCards((prev) => {
        const next = { ...prev };
        for (const card of incoming) {
          if ((choseAtRef.current[card.id] ?? 0) > readAt) continue;
          next[card.id] = card;
        }
        return next;
      });
    },
    []
  );

  const handleFiles = useCallback((chosen: File[]) => {
    setAttachError(null);

    // A zip, or more files than one message carries, is a batch, and a batch
    // belongs in the ride archive import: it skips rides already in and
    // rebuilds fitness once. The chat would do neither, so it takes none of
    // them rather than half.
    const zip = chosen.find(isZip);
    const picked = chosen.filter((file) => !isZip(file));
    if (picked.length > MAX_ATTACHMENTS) {
      setAttachError({
        text: "Three files is the limit for one message. For a batch, put the files in one zip and choose it in Settings, then Data in, then Ride archive: rides already in are skipped.",
        dataIn: true,
      });
      return;
    }
    if (zip) {
      setAttachError({
        text: `${zip.name} is a zip of rides, and the chat takes single ride files. Choose it in Settings, then Data in, then Ride archive instead: rides already in are skipped.`,
        dataIn: true,
      });
      if (!picked.length) return;
    }

    const room = MAX_ATTACHMENTS - attachmentsRef.current.length;
    if (room <= 0) {
      setAttachError({
        text: "Three files is the limit for one message. Send these, then attach the rest.",
      });
      return;
    }
    if (picked.length > room) {
      setAttachError({
        text: `Only the first ${room === 1 ? "file" : `${room} files`} went on. Three is the limit for one message, so send these, then attach the rest.`,
      });
    }

    for (const file of picked.slice(0, room)) {
      const key = `attachment-${++attachKeyRef.current}`;
      setAttachments((prev) => [
        ...prev,
        { key, filename: file.name, status: "uploading" },
      ]);

      // An open chat claims the file from the start; a new one claims it
      // when the message carrying it is sent.
      chat
        .uploadAttachment(file, activeSessionId)
        .then((saved) => {
          setAttachments((prev) =>
            prev.map((a) =>
              a.key === key
                ? {
                    ...a,
                    status: "ready" as const,
                    id: saved.id,
                    filename: saved.filename || a.filename,
                    summary: saved.summary,
                    card: saved.card ?? null,
                  }
                : a
            )
          );
        })
        .catch((error: unknown) => {
          // A file that would not parse is not attached at all: a chip that
          // cannot be sent is worse than no chip.
          setAttachments((prev) => prev.filter((a) => a.key !== key));
          const said = error instanceof Error ? error.message : "";
          setAttachError({
            text: said
              ? `${file.name}: ${said}`
              : `${file.name} could not be read. Try the original file from your head unit.`,
            dataIn: said.includes("Data in"),
          });
        });
    }
  }, [activeSessionId]);

  const removeAttachment = useCallback((key: string) => {
    setAttachments((prev) => prev.filter((a) => a.key !== key));
    setAttachError(null);
  }, []);

  // Both send paths hand over the same chips, so the take is shared: read the
  // settled ids, clear the composer, and let the caller stamp the files onto
  // the rider's message. Their cards join the thread's as they go.
  const takeAttachments = useCallback(() => {
    const settled = attachmentsRef.current.filter(
      (a) => a.status === "ready" && a.id
    );
    setAttachments([]);
    setAttachError(null);
    mergeCards(
      settled.flatMap((a) => (a.card ? [a.card] : [])),
      Date.now()
    );
    return {
      ids: settled.map((a) => a.id as string),
      names: settled.map((a) => a.filename),
    };
  }, [mergeCards]);

  // The chat the rider just wrote in goes to the top at once, under any pins,
  // the way a messenger moves a thread; the refetch after the reply settles
  // the server's order. Moved by position rather than by timestamp: the
  // server's times carry no zone, and the browser would read them as local.
  const bumpSession = useCallback(
    (id: string, fresh?: ChatSession) => {
      queryClient.setQueriesData<ChatSession[]>(
        { queryKey: ["chat-sessions"] },
        (list) => {
          if (!list) return list;
          const found = list.find((s) => s.id === id) || fresh;
          if (!found) return list;
          const moved = {
            ...found,
            last_message_at: new Date().toISOString(),
            message_count: found.message_count + 1,
          };
          const rest = list.filter((s) => s.id !== id);
          const at = moved.pinned ? 0 : rest.filter((s) => s.pinned).length;
          return [...rest.slice(0, at), moved, ...rest.slice(at)];
        }
      );
    },
    [queryClient]
  );

  // The send button comes back when the reply is in. The server names a new
  // chat a few seconds later, just before the stream closes, so the list is
  // fetched again when it does.
  const refreshChatList = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
  }, [queryClient]);

  // Voice send handler (defined below, used by hook)
  const handleVoiceSend = useCallback(
    async (transcript: string) => {
      if (!transcript.trim() || streaming) return;

      let sessionId = activeSessionId;
      let created: ChatSession | undefined;

      // Reset audio mute — new message, allow audio again
      audioMutedRef.current = false;

      // Create session if needed
      if (!sessionId) {
        created = await chat.createSession();
        sessionId = created.id;
        setActiveSessionId(created.id);
        queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      }

      const handedOver = takeAttachments();
      bumpSession(sessionId, created);

      setMessages((prev) => [
        ...prev,
        {
          role: "user" as const,
          content: transcript.trim(),
          attachments: handedOver.names.length ? handedOver.names : undefined,
          attachmentIds: handedOver.ids.length ? handedOver.ids : undefined,
        },
      ]);
      setStreaming(true);

      // Add empty assistant message
      setMessages((prev) => [
        ...prev,
        { role: "assistant" as const, content: "" },
      ]);

      try {
        let assistantContent = "";
        for await (const chunk of chat.sendVoiceMessage(
          sessionId,
          transcript.trim(),
          handedOver.ids,
          refreshChatList
        )) {
          if (chunk.type === "status") {
            setStatus(chunk.content ?? null);
          } else if (chunk.type === "text") {
            // Text always streams fully — never interrupted
            setStatus(null);
            assistantContent += chunk.content;
            setMessages((prev) => {
              const updated = [...prev];
              updated[updated.length - 1] = {
                role: "assistant",
                content: assistantContent,
              };
              return updated;
            });
          } else if (chunk.type === "audio" && chunk.content) {
            // Only play audio if not muted by interruption or preference
            if (!audioMutedRef.current && !audioOffRef.current) {
              playAudioChunk(chunk.content);
            }
          } else if (chunk.type === "plan_updated") {
            // The coach changed the plan: refresh the calendar and Today,
            // which otherwise holds its session for five minutes. A goal it
            // filed can rebuild the whole plan, so the plan and goals too.
            refreshPlan();
          }
        }
      } catch (error) {
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            role: "assistant",
            content: sendFailureMessage(error),
          };
          return updated;
        });
      }

      setStreaming(false);
      setStatus(null);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeSessionId, streaming]
  );

  // Voice chat hook
  const {
    isListening,
    isSpeaking,
    isSupported: voiceSupported,
    interimTranscript,
    startListening,
    stopListening,
    playAudioChunk,
    stopAudio,
  } = useVoiceChat({
    onTranscript: handleVoiceSend,
  });

  // Persistent "text only" preference: some riders never want audio.
  const [audioOff, setAudioOff] = useState(false);
  useEffect(() => {
    setAudioOff(localStorage.getItem("forma:coach-audio-off") === "1");
  }, []);
  const toggleAudioOff = () => {
    setAudioOff((v) => {
      const next = !v;
      localStorage.setItem("forma:coach-audio-off", next ? "1" : "0");
      if (next) stopAudio(); // silences immediately, mid-sentence included
      return next;
    });
  };
  const audioOffRef = useRef(audioOff);
  useEffect(() => {
    audioOffRef.current = audioOff;
  }, [audioOff]);

  // ⌥Space toggles the mic — the desktop voice shortcut.
  // (The Mac dictation key is OS-owned and never reaches a web page, but it
  // still types into the input via Apple's own engine as a bonus path.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey && e.code === "Space") {
        e.preventDefault();
        if (isListening) {
          stopListening();
        } else if (voiceSupported && !streaming) {
          setVoiceMode(true);
          startListening();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isListening, voiceSupported, streaming, startListening, stopListening]);

  // Determine voice indicator mode
  const voiceIndicatorMode = isListening
    ? "listening"
    : streaming && voiceMode
      ? "processing"
      : isSpeaking
        ? "speaking"
        : "idle";

  // Fetch goal data if goal_id is in URL
  const { data: goalData } = useQuery({
    queryKey: ["goal-for-coach", goalId],
    queryFn: () => goalsApi.get(goalId!),
    enabled: !!goalId && !goalMessagePrefilled,
  });

  // Pre-fill message when goal data loads
  useEffect(() => {
    if (goalData && !goalMessagePrefilled) {
      const parts: string[] = [];

      if (isDebrief && goalData.assessment_completed_at) {
        // Debrief mode — post-event conversation
        parts.push(`I just completed ${goalData.event_name}.`);

        if (goalData.finish_time_seconds) {
          const h = Math.floor(goalData.finish_time_seconds / 3600);
          const m = Math.floor((goalData.finish_time_seconds % 3600) / 60);
          parts.push(`I finished in ${h}h ${m}m.`);
        }

        if (goalData.overall_satisfaction) {
          parts.push(`I rated my satisfaction ${goalData.overall_satisfaction}/10`);
        }
        if (goalData.perceived_exertion) {
          parts.push(`and effort ${goalData.perceived_exertion}/10.`);
        }

        const ad = goalData.assessment_data as Record<string, unknown> | null;
        if (ad?.went_well) {
          parts.push(`What went well: ${ad.went_well}.`);
        }
        if (ad?.to_improve) {
          parts.push(`What to improve: ${ad.to_improve}.`);
        }

        parts.push("Can you help me debrief this event?");
      } else {
        // Preparation mode
        parts.push(
          `I'd like your help preparing for ${goalData.event_name} on ${goalData.event_date}.`
        );
        parts.push(
          `It's a ${goalData.event_type.replace(/_/g, " ")} (${goalData.priority.replace(/_/g, " ")} priority).`
        );

        if (goalData.route_data) {
          const rd = goalData.route_data;
          const routeParts: string[] = [];
          if (rd.total_distance_km)
            routeParts.push(`${rd.total_distance_km.toFixed(1)}km`);
          if (rd.elevation_gain_m)
            routeParts.push(`${Math.round(rd.elevation_gain_m)}m climbing`);
          if (routeParts.length > 0) {
            parts.push(`The route is ${routeParts.join(" with ")}.`);
          }
        }

        if (goalData.target_duration_minutes) {
          const hours = Math.floor(goalData.target_duration_minutes / 60);
          const mins = goalData.target_duration_minutes % 60;
          const durStr = hours > 0 ? `${hours}h ${mins}m` : `${mins} minutes`;
          parts.push(`My target duration is ${durStr}.`);
        }

        if (goalData.days_until !== null && goalData.days_until > 0) {
          parts.push(`I have ${goalData.days_until} days to prepare.`);
        }

        parts.push("What should my preparation look like?");
      }

      setInput(parts.join(" "));
      setGoalMessagePrefilled(true);

      // Focus the input
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [goalData, goalMessagePrefilled, isDebrief]);

  // Session management UI state
  const [showArchived, setShowArchived] = useState(false);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // Load sessions list (pinned first, then the latest message first, from
  // the API; archived on demand)
  const { data: sessions } = useQuery({
    queryKey: ["chat-sessions", showArchived],
    queryFn: () => chat.getSessions(showArchived),
  });

  const updateSession = useMutation({
    mutationFn: ({ id, update }: { id: string; update: Parameters<typeof chat.updateSession>[1] }) =>
      chat.updateSession(id, update),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
  });

  const removeSession = useMutation({
    mutationFn: (id: string) => chat.deleteSession(id),
    onSuccess: (_, id) => {
      if (id === activeSessionId) {
        setActiveSessionId(null);
        setMessages([]);
      }
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
  });

  const startRename = (id: string, current: string | null) => {
    setRenamingId(id);
    setRenameValue(current ? chatTitle(current) : "");
    setMenuFor(null);
  };

  const commitRename = () => {
    if (renamingId && renameValue.trim()) {
      updateSession.mutate({ id: renamingId, update: { title: renameValue.trim() } });
    }
    setRenamingId(null);
  };

  // Load active session messages (latest window; older pages on demand)
  const { data: sessionData, dataUpdatedAt: sessionReadAt } = useQuery({
    queryKey: ["chat-session", activeSessionId],
    queryFn: () => chat.getSession(activeSessionId!, { limit: 60 }),
    enabled: !!activeSessionId,
  });

  // The conversation continues: opening COACH resumes the thread with the
  // latest message instead of an empty page, the way a messenger opens. A
  // pin does not win by default, and a leftover empty "New Chat" never takes
  // over the page. "New chat" stays for deliberate splits.
  useEffect(() => {
    if (resumedRef.current || activeSessionId || goalId || isDebrief) return;
    if (!sessions || sessions.length === 0) return;
    const live = sessions.filter((s) => !s.archived_at);
    if (!live.length) return;
    const written = live.filter((s) => s.message_count > 0);
    const latest = [...(written.length ? written : live)].sort((a, b) =>
      lastActive(b).localeCompare(lastActive(a))
    )[0];
    resumedRef.current = true;
    setActiveSessionId(latest.id);
  }, [sessions, activeSessionId, goalId, isDebrief]);

  // When session data loads, set messages, and the cards of the files they
  // carried
  useEffect(() => {
    if (sessionData?.messages) {
      setMessages(
        sessionData.messages.map((m) => ({
          role: m.role,
          content: m.content,
          id: m.id,
          created_at: m.created_at,
          attachmentIds: m.attachment_ids?.length ? m.attachment_ids : undefined,
        }))
      );
      setHasEarlier(!!sessionData.has_more);
      mergeCards(sessionData.attachments, sessionReadAt);
    }
    // sessionReadAt moves with sessionData; the data is what to react to.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionData]);

  // Page backwards through history as the rider scrolls up
  const loadEarlier = async () => {
    if (!activeSessionId || loadingEarlier) return;
    const oldest = messages.find((m) => m.id);
    if (!oldest?.id) return;
    setLoadingEarlier(true);
    try {
      const askedAt = Date.now();
      const older = await chat.getSession(activeSessionId, {
        limit: 60,
        before: oldest.id,
      });
      setMessages((prev) => [
        ...older.messages.map((m) => ({
          role: m.role,
          content: m.content,
          id: m.id,
          created_at: m.created_at,
          attachmentIds: m.attachment_ids?.length ? m.attachment_ids : undefined,
        })),
        ...prev,
      ]);
      setHasEarlier(!!older.has_more);
      mergeCards(older.attachments, askedAt);
    } finally {
      setLoadingEarlier(false);
    }
  };

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // A choice on a card reaches every upload of the same file, so when this
  // chat holds another copy its cards are read again. Only the cards: the
  // thread on screen stays as it is, earlier pages and a streaming reply
  // included. The cached session is marked behind either way, so the next
  // time the chat opens it is read fresh.
  const rereadCards = useCallback(async () => {
    const sessionId = activeSessionId;
    if (!sessionId) return;
    const askedAt = Date.now();
    try {
      const fresh = await chat.getSession(sessionId, { limit: 200 });
      mergeCards(fresh.attachments, askedAt);
    } catch {
      // The card that was chosen on is right already; the others catch up
      // the next time the chat loads.
    }
  }, [activeSessionId, mergeCards]);

  const onCardDecided = useCallback(
    (next: ChatAttachmentCard, before: ChatAttachmentCard) => {
      choseAtRef.current[next.id] = Date.now();
      setCards((prev) => ({ ...prev, [next.id]: next }));
      queryClient.invalidateQueries({
        queryKey: ["chat-session", activeSessionId],
        refetchType: "none",
      });
      // A save adds a ride and rebuilds fitness on the server, so Rides, the
      // fitness chart and everything read from them are behind it.
      if (next.view === "saved" && before.view !== "saved") {
        refreshRideData(queryClient);
      }
      const copied = Object.values(cards).some(
        (c) =>
          c.id !== next.id &&
          c.kind === next.kind &&
          !!c.start_time &&
          c.start_time === next.start_time
      );
      if (copied) void rereadCards();
    },
    [activeSessionId, cards, queryClient, rereadCards]
  );

  // Create new session
  const createSession = useMutation({
    mutationFn: () => chat.createSession(),
    onSuccess: (session) => {
      setActiveSessionId(session.id);
      setMessages([]);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    },
  });

  // Send message with streaming
  const handleSend = async () => {
    if (streaming) return;
    // A file on its own is a fair question: "here, look at this".
    if (!input.trim() && readyAttachments.length === 0) return;
    if (uploading) {
      setAttachError({ text: "One file is still being read. It will only be a moment." });
      return;
    }

    let sessionId = activeSessionId;
    let created: ChatSession | undefined;

    // Create session if none active
    if (!sessionId) {
      created = await chat.createSession();
      sessionId = created.id;
      setActiveSessionId(created.id);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    }

    const handedOver = takeAttachments();
    // A file sent with no words goes with a line that asks nothing, so the
    // coach asks what the rider wants from it (see attachmentOnlyMessage).
    const userMessage =
      input.trim() ||
      (handedOver.ids.length ? attachmentOnlyMessage(handedOver.ids.length) : "");
    setInput("");
    bumpSession(sessionId, created);
    setMessages((prev) => [
      ...prev,
      {
        role: "user",
        content: userMessage,
        attachments: handedOver.names.length ? handedOver.names : undefined,
        attachmentIds: handedOver.ids.length ? handedOver.ids : undefined,
      },
    ]);
    setStreaming(true);

    // Add empty assistant message
    setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

    try {
      let assistantContent = "";
      for await (const chunk of chat.sendMessage(
        sessionId,
        userMessage,
        handedOver.ids,
        refreshChatList
      )) {
        if (chunk.type === "status") {
          setStatus(chunk.content ?? null);
        } else if (chunk.type === "text") {
          setStatus(null);
          assistantContent += chunk.content;
          setMessages((prev) => {
            const updated = [...prev];
            updated[updated.length - 1] = {
              role: "assistant",
              content: assistantContent,
            };
            return updated;
          });
        } else if (chunk.type === "plan_updated") {
          // The coach changed the plan: refresh the calendar and Today,
          // which otherwise holds its session for five minutes. A goal it
          // filed can rebuild the whole plan, so the plan and goals too.
          refreshPlan();
        }
      }
    } catch (error) {
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          role: "assistant",
          content: sendFailureMessage(error),
        };
        return updated;
      });
    }

    setStreaming(false);
    setStatus(null);
    queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const [showSessions, setShowSessions] = useState(false);

  return (
    // Fill whatever height the shell has left rather than computing it from
    // 100vh: on iOS Safari that unit is the toolbar-hidden viewport, so the
    // composer ended up behind the toolbar, and the calc could not account for
    // the verify-email banner or the wrapper's own padding either.
    <div className="flex min-h-0 flex-1 gap-0 md:gap-4">
      {/* Sessions Sidebar - hidden on mobile, toggle with button */}
      <div
        className={cn(
          "shrink-0 overflow-y-auto rounded-md border border-vb-border-subtle bg-vb-surface",
          showSessions
            ? "absolute inset-x-4 top-16 z-30 max-h-[60vh] md:static md:inset-auto md:z-auto md:max-h-none md:w-64"
            : "hidden md:block md:w-64"
        )}
      >
        <div className="border-b border-vb-border-subtle p-3">
          <button
            onClick={() => {
              createSession.mutate();
              setShowSessions(false);
            }}
            className="flex w-full items-center justify-center gap-2 rounded-sm bg-vb-forest px-3 py-2 text-sm font-medium text-white hover:bg-vb-forest-soft"
          >
            <Plus className="h-4 w-4" /> New Chat
          </button>
        </div>
        <div className="divide-y divide-vb-border-subtle">
          {sessions?.length === 0 && (
            <p className="px-4 py-6 text-center text-xs text-vb-text-muted">
              Start a conversation with your coach
            </p>
          )}
          {sessions?.map((session) => (
            <div
              key={session.id}
              className={cn(
                "group relative flex w-full items-center gap-2 px-3 py-3 text-left text-sm transition-colors",
                session.archived_at && "opacity-60",
                session.id === activeSessionId
                  ? "bg-vb-sage-tint text-vb-forest"
                  : "text-vb-text-dim hover:bg-vb-surface-raised hover:text-vb-text"
              )}
            >
              {session.pinned ? (
                <Pin className="h-3.5 w-3.5 shrink-0 fill-current text-vb-red" />
              ) : (
                <MessageCircle className="h-4 w-4 shrink-0" />
              )}
              {renamingId === session.id ? (
                <input
                  autoFocus
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onBlur={commitRename}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitRename();
                    if (e.key === "Escape") setRenamingId(null);
                  }}
                  className="min-w-0 flex-1 border border-vb-border bg-vb-surface px-1.5 py-0.5 text-xs text-vb-text outline-none focus:border-vb-red"
                />
              ) : (
                <button
                  onClick={() => {
                    setActiveSessionId(session.id);
                    setShowSessions(false);
                  }}
                  className="min-w-0 flex-1 text-left"
                >
                  <p className="flex items-center gap-1 truncate text-xs font-medium">
                    {session.starred && (
                      <Star className="h-3 w-3 shrink-0 fill-current text-vb-text" />
                    )}
                    <span className="truncate">{chatTitle(session.title)}</span>
                  </p>
                  <p className="text-[10px] text-vb-text-muted">
                    {session.archived_at
                      ? "Archived"
                      : messageCount(session.message_count)}
                  </p>
                </button>
              )}

              {/* Quiet row menu */}
              <button
                onClick={() =>
                  setMenuFor(menuFor === session.id ? null : session.id)
                }
                className="shrink-0 rounded-sm p-1 text-vb-text-muted opacity-0 transition-opacity hover:text-vb-text focus:opacity-100 group-hover:opacity-100"
                aria-label="Chat options"
              >
                <MoreHorizontal className="h-4 w-4" />
              </button>

              {menuFor === session.id && (
                <>
                  <div
                    className="fixed inset-0 z-30"
                    onClick={() => setMenuFor(null)}
                  />
                  <div className="absolute right-2 top-10 z-40 w-40 border border-vb-border bg-vb-surface-raised py-1 shadow-none">
                    <button
                      onClick={() => startRename(session.id, session.title)}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-vb-text-dim hover:bg-vb-sunken hover:text-vb-text"
                    >
                      <Pencil className="h-3.5 w-3.5" /> Rename
                    </button>
                    <button
                      onClick={() => {
                        updateSession.mutate({
                          id: session.id,
                          update: { pinned: !session.pinned },
                        });
                        setMenuFor(null);
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-vb-text-dim hover:bg-vb-sunken hover:text-vb-text"
                    >
                      <Pin className="h-3.5 w-3.5" />
                      {session.pinned ? "Unpin" : "Pin"}
                    </button>
                    <button
                      onClick={() => {
                        updateSession.mutate({
                          id: session.id,
                          update: { starred: !session.starred },
                        });
                        setMenuFor(null);
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-vb-text-dim hover:bg-vb-sunken hover:text-vb-text"
                    >
                      <Star className="h-3.5 w-3.5" />
                      {session.starred ? "Unstar" : "Star"}
                    </button>
                    <button
                      onClick={() => {
                        updateSession.mutate({
                          id: session.id,
                          update: { archived: !session.archived_at },
                        });
                        setMenuFor(null);
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-vb-text-dim hover:bg-vb-sunken hover:text-vb-text"
                    >
                      {session.archived_at ? (
                        <ArchiveRestore className="h-3.5 w-3.5" />
                      ) : (
                        <Archive className="h-3.5 w-3.5" />
                      )}
                      {session.archived_at ? "Unarchive" : "Archive"}
                    </button>
                    <button
                      onClick={() => {
                        setMenuFor(null);
                        if (
                          window.confirm(
                            "Delete this chat for good? The conversation and the ride files shared in it are gone. Forma keeps what it learned, and rides you saved stay in Rides."
                          )
                        ) {
                          removeSession.mutate(session.id);
                        }
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-vb-red hover:bg-vb-sunken"
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Delete
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
        </div>

        {/* Archived toggle */}
        <div className="border-t border-vb-border-subtle px-4 py-2">
          <button
            onClick={() => setShowArchived((v) => !v)}
            className="f-kicker text-vb-text-muted transition-colors hover:text-vb-text"
          >
            {showArchived ? "Hide archived" : "Show archived"}
          </button>
        </div>
      </div>

      {/* Mobile sessions overlay backdrop */}
      {showSessions && (
        <div
          className="fixed inset-0 z-20 bg-black/40 md:hidden"
          onClick={() => setShowSessions(false)}
        />
      )}

      {/* Chat Area */}
      <div className="flex flex-1 flex-col overflow-hidden rounded-md border border-vb-border-subtle bg-vb-surface">
        {/* Mobile chat header */}
        <div className="flex items-center gap-2 border-b border-vb-border-subtle px-3 py-2 md:hidden">
          <button
            onClick={() => setShowSessions(!showSessions)}
            className="rounded-sm p-1.5 text-vb-text-dim hover:bg-vb-surface-raised hover:text-vb-text"
          >
            <MessageCircle className="h-5 w-5" />
          </button>
          <span className="text-sm font-medium text-vb-text-dim">
            {chatTitle(
              sessions?.find((s) => s.id === activeSessionId)?.title,
              `Coach ${coach}`
            )}
          </span>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-3 sm:p-5">
          {messages.length === 0 && (
            <div className="flex h-full items-center justify-center">
              <div className="text-center">
                <CoachGlyph className="mx-auto h-20 w-20" />
                <h3 className="mt-4 font-display text-lg font-semibold tracking-[-0.01em] text-vb-text">
                  Coach {coach}
                </h3>
                <p className="mt-1 max-w-sm text-sm text-vb-text-dim">
                  Training, racing, your head, your life: ask me anything. I
                  remember what you tell me, and you can see everything
                  I&apos;ve kept in{" "}
                  <Link href="/dashboard/brain" className="text-vb-forest hover:underline">
                    Brain
                  </Link>
                  .
                </p>
                <StarterChips
                  starters={starters.slice(0, 6)}
                  onPick={(ask) => {
                    setInput(ask);
                    inputRef.current?.focus();
                  }}
                  className="mt-6 flex flex-wrap justify-center gap-2"
                />
                {voiceSupported && (
                  <p className="mt-4 text-xs text-vb-text-muted">
                    <Mic className="mr-1 inline h-3 w-3" />
                    Tap the mic to talk with your coach
                  </p>
                )}
              </div>
            </div>
          )}

          <div className="space-y-4">
            {hasEarlier && (
              <div className="flex justify-center pb-2">
                <button
                  onClick={loadEarlier}
                  disabled={loadingEarlier}
                  className="f-kicker text-vb-text-muted transition-colors hover:text-vb-text"
                >
                  {loadingEarlier ? "Loading…" : "↑ Earlier"}
                </button>
              </div>
            )}
            {messages.map((msg, i) => {
              // A saved-empty reply renders as a ghost bubble — never show it.
              if (
                msg.role === "assistant" &&
                !msg.content &&
                !(streaming && i === messages.length - 1)
              ) {
                return null;
              }
              // Day divider: the thread reads like a training diary.
              const day = msg.created_at?.slice(0, 10);
              const prevDay = messages[i - 1]?.created_at?.slice(0, 10);
              const showDivider = day && day !== prevDay;
              // The cards of the files a rider message shared go under it. A
              // file with no card keeps its name in the bubble instead.
              const ids = msg.role === "user" ? msg.attachmentIds ?? [] : [];
              const shared = ids.flatMap((id) => (cards[id] ? [cards[id]] : []));
              const named = (msg.attachments ?? []).filter(
                (_, k) => !(ids[k] && cards[ids[k]])
              );
              const bubble = (inColumn: boolean) => (
                <div
                  className={cn(
                    "rounded-md px-3 py-2.5 text-sm sm:px-4 sm:py-3",
                    inColumn ? "max-w-full" : "max-w-[90%] sm:max-w-[80%]",
                    // rider speaks in ink; flamme stays the coach's colour
                    msg.role === "user"
                      ? "bg-vb-text text-white"
                      : "bg-vb-sage-tint text-vb-text"
                  )}
                >
                  {msg.role === "assistant" ? (
                    <div className="prose prose-sm max-w-none prose-headings:text-vb-text prose-strong:text-vb-text prose-em:text-vb-clay prose-em:not-italic prose-li:marker:text-vb-forest prose-a:text-vb-forest">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                      {streaming && i === messages.length - 1 && (
                        <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-vb-forest" />
                      )}
                    </div>
                  ) : (
                    <>
                      {msg.content && <p>{msg.content}</p>}
                      {/* What the rider handed over, kept on the record */}
                      {named.length > 0 && (
                        <ul
                          className={cn(
                            "space-y-1",
                            msg.content && "mt-2 border-t border-white/20 pt-2"
                          )}
                        >
                          {named.map((name) => (
                            <li
                              key={name}
                              className="flex items-center gap-1.5 text-[11px] text-white/75"
                            >
                              <Paperclip className="h-3 w-3 shrink-0" />
                              <span className="truncate">{name}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </>
                  )}
                </div>
              );
              return (
                <React.Fragment key={msg.id || i}>
                  {showDivider && (
                    <div className="flex items-center gap-3 py-2">
                      <span className="h-px flex-1 bg-vb-border-subtle" />
                      <span className="f-kicker text-vb-text-muted">
                        {new Date(day!).toLocaleDateString("en-GB", {
                          weekday: "short",
                          day: "numeric",
                          month: "short",
                        })}
                      </span>
                      <span className="h-px flex-1 bg-vb-border-subtle" />
                    </div>
                  )}
                  <div
                    className={cn(
                      "flex gap-3",
                      msg.role === "user" ? "justify-end" : "justify-start"
                    )}
                  >
                {msg.role === "assistant" &&
                  !(
                    status &&
                    !msg.content &&
                    streaming &&
                    i === messages.length - 1
                  ) && (
                    <span className="flex h-7 w-7 shrink-0 items-center justify-center">
                      <CoachDot
                        state={
                          streaming && i === messages.length - 1
                            ? "pulsing"
                            : "still"
                        }
                        size="10px"
                      />
                    </span>
                  )}
                {/* C2 · active · pulsing — the dot breathes while the coach
                    works. Before the first word arrives there is no bubble,
                    just the brand's thinking state: dot + mono status line. */}
                {msg.role === "assistant" &&
                !msg.content &&
                streaming &&
                i === messages.length - 1 ? (
                  status ? (
                    <div className="flex items-center gap-4 self-center py-1">
                      <CadenceSpinner
                        size={34}
                        className="text-vb-red"
                        title="Analysing your ride"
                      />
                      <span className="max-w-md text-sm leading-relaxed text-vb-text-dim">
                        {status}&hellip;
                      </span>
                    </div>
                  ) : (
                    <p className="f-kicker self-center text-vb-text-muted">
                      {coach} is thinking&hellip;
                    </p>
                  )
                ) : shared.length > 0 ? (
                  // The cards for the files it shared sit under the rider's
                  // message, on the rider's side of the thread.
                  <div className="flex w-full min-w-0 max-w-[90%] flex-col items-end gap-2 sm:max-w-[80%]">
                    {bubble(true)}
                    {shared.map((card) => (
                      <FileCard
                        key={card.id}
                        card={card}
                        onDecided={onCardDecided}
                        onStale={rereadCards}
                        className="max-w-sm"
                      />
                    ))}
                  </div>
                ) : (
                  bubble(false)
                )}
                    {msg.role === "user" && (
                      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-vb-sunken">
                        <User className="h-4 w-4 text-vb-text" />
                      </div>
                    )}
                  </div>
                </React.Fragment>
              );
            })}
            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Input */}
        <div className="border-t border-vb-border-subtle p-4">
          {/* Voice indicator, shown during voice interactions */}
          {voiceMode && voiceIndicatorMode !== "idle" && (
            <VoiceIndicator
              mode={voiceIndicatorMode}
              interimTranscript={interimTranscript}
            />
          )}

          {/* Doors into the coach's range, ranked by what's being discussed */}
          {!input && attachments.length === 0 && (
            <StarterChips
              starters={starters}
              scrollable
              onPick={(ask) => {
                setInput(ask);
                inputRef.current?.focus();
              }}
              className="mb-3"
            />
          )}

          <AttachmentChips
            attachments={attachments}
            onRemove={removeAttachment}
          />

          {attachError && (
            <p className="mb-3 border-l-2 border-vb-red pl-3 text-xs text-vb-text-dim">
              {attachError.text}
              {attachError.dataIn && (
                <>
                  {" "}
                  <Link
                    href={DATA_IN_HREF}
                    className="rounded-sm font-medium text-vb-text underline underline-offset-2 hover:text-vb-red focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-vb-red"
                  >
                    Open Data&nbsp;in
                  </Link>
                </>
              )}
            </p>
          )}

          <div className="flex items-end gap-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                attachments.length
                  ? "Ask about the ride, or just send it…"
                  : "Ask your coach anything…"
              }
              rows={1}
              className="max-h-32 flex-1 resize-none rounded-sm border border-vb-border bg-vb-surface px-4 py-2.5 text-sm text-vb-text placeholder-vb-text-muted focus:border-vb-forest focus:outline-none"
              style={{
                height: "auto",
                minHeight: "42px",
              }}
              onInput={(e) => {
                const el = e.target as HTMLTextAreaElement;
                el.style.height = "auto";
                el.style.height = Math.min(el.scrollHeight, 128) + "px";
              }}
            />

            {/* Hand a ride file over: GPX, FIT or TCX, gzipped or not */}
            <AttachButton
              onFiles={handleFiles}
              disabled={streaming || attachments.length >= MAX_ATTACHMENTS}
            />

            {/* Coach audio on/off — some riders want text only. Also stops
                playback mid-sentence when switched off. */}
            {voiceSupported && (
              <button
                onClick={toggleAudioOff}
                title={
                  audioOff
                    ? "Coach voice is off. Click to hear Forma"
                    : "Mute coach voice (text only)"
                }
                aria-label={audioOff ? "Unmute coach voice" : "Mute coach voice"}
                className={cn(
                  "flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-sm border transition-colors",
                  audioOff
                    ? "border-vb-border bg-vb-sunken text-vb-text-muted"
                    : "border-vb-border bg-vb-surface text-vb-text-dim hover:text-vb-text"
                )}
              >
                {audioOff ? (
                  <VolumeX className="h-4 w-4" />
                ) : (
                  <Volume2 className="h-4 w-4" />
                )}
              </button>
            )}

            {/* Voice button — click or ⌥space */}
            <VoiceButton
              isListening={isListening}
              isSpeaking={isSpeaking}
              isSupported={voiceSupported}
              isProcessing={streaming}
              onToggle={() => {
                if (isListening) {
                  stopListening();
                } else if (isSpeaking) {
                  // Interrupt coach: stop audio, mute future chunks, start listening
                  stopAudio();
                  audioMutedRef.current = true;
                  setVoiceMode(true);
                  startListening();
                } else {
                  setVoiceMode(true);
                  startListening();
                }
              }}
            />

            {/* Send button */}
            <button
              onClick={handleSend}
              disabled={
                (!input.trim() && readyAttachments.length === 0) ||
                streaming ||
                uploading
              }
              className="flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-sm bg-vb-forest text-white hover:bg-vb-forest-soft disabled:opacity-50"
            >
              <Send className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function CoachPage() {
  return (
    <Suspense
      fallback={
        <div className="flex h-[calc(100vh-3rem)] items-center justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-vb-forest border-t-transparent" />
        </div>
      }
    >
      <CoachPageInner />
    </Suspense>
  );
}
