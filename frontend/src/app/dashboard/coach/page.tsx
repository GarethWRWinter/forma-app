"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import React, { useState, useRef, useEffect, useCallback, Suspense } from "react";
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
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { chat, goals as goalsApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { CoachDot, CoachGlyph } from "@/components/ui/coach-glyph";
import { StarterChips, useCoachStarters } from "@/components/coach/coach-starters";
import { useVoiceChat } from "@/hooks/useVoiceChat";
import { useAuth } from "@/lib/auth-context";
import { VoiceButton } from "@/components/voice/VoiceButton";
import { VoiceIndicator } from "@/components/voice/VoiceIndicator";

function CoachPageInner() {
  const { user: authUser } = useAuth();
  const coach = authUser?.coach_name || "Forma";
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const goalId = searchParams.get("goal_id");

  const isDebrief = searchParams.get("debrief") === "true";

  // Handoffs from briefings and other surfaces arrive as ?ask=... and
  // prefill the input; the rider presses send, nothing is sent for them.
  const askParam = searchParams.get("ask");
  useEffect(() => {
    if (askParam) setInput(askParam);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [askParam]);

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<
    { role: "user" | "assistant"; content: string; id?: string; created_at?: string }[]
  >([]);
  const [hasEarlier, setHasEarlier] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const resumedRef = useRef(false);
  const [input, setInput] = useState("");
  const starters = useCoachStarters(5);
  const [streaming, setStreaming] = useState(false);
  const [goalMessagePrefilled, setGoalMessagePrefilled] = useState(false);
  const [voiceMode, setVoiceMode] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const audioMutedRef = useRef(false);

  // Voice send handler (defined below, used by hook)
  const handleVoiceSend = useCallback(
    async (transcript: string) => {
      if (!transcript.trim() || streaming) return;

      let sessionId = activeSessionId;

      // Reset audio mute — new message, allow audio again
      audioMutedRef.current = false;

      // Create session if needed
      if (!sessionId) {
        const session = await chat.createSession();
        sessionId = session.id;
        setActiveSessionId(session.id);
        queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
      }

      setMessages((prev) => [
        ...prev,
        { role: "user" as const, content: transcript.trim() },
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
          transcript.trim()
        )) {
          if (chunk.type === "text") {
            // Text always streams fully — never interrupted
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
            // Coach modified the training plan — refresh calendar data
            queryClient.invalidateQueries({ queryKey: ["workouts-week"] });
          }
        }
      } catch {
        setMessages((prev) => {
          const updated = [...prev];
          updated[updated.length - 1] = {
            role: "assistant",
            content: "Sorry, I had trouble connecting. Please try again.",
          };
          return updated;
        });
      }

      setStreaming(false);
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

  // Load sessions list (pinned first from the API; archived on demand)
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
    setRenameValue(current || "");
    setMenuFor(null);
  };

  const commitRename = () => {
    if (renamingId && renameValue.trim()) {
      updateSession.mutate({ id: renamingId, update: { title: renameValue.trim() } });
    }
    setRenamingId(null);
  };

  // Load active session messages (latest window; older pages on demand)
  const { data: sessionData } = useQuery({
    queryKey: ["chat-session", activeSessionId],
    queryFn: () => chat.getSession(activeSessionId!, { limit: 60 }),
    enabled: !!activeSessionId,
  });

  // The conversation continues: opening COACH resumes the most recent
  // thread instead of an empty page. "New chat" stays for deliberate splits.
  useEffect(() => {
    if (resumedRef.current || activeSessionId || goalId || isDebrief) return;
    if (!sessions || sessions.length === 0) return;
    const live = sessions.filter((s) => !s.archived_at);
    if (!live.length) return;
    const latest = [...live].sort((a, b) =>
      (b.updated_at || b.created_at || "").localeCompare(
        a.updated_at || a.created_at || ""
      )
    )[0];
    resumedRef.current = true;
    setActiveSessionId(latest.id);
  }, [sessions, activeSessionId, goalId, isDebrief]);

  // When session data loads, set messages
  useEffect(() => {
    if (sessionData?.messages) {
      setMessages(
        sessionData.messages.map((m) => ({
          role: m.role,
          content: m.content,
          id: m.id,
          created_at: m.created_at,
        }))
      );
      setHasEarlier(!!sessionData.has_more);
    }
  }, [sessionData]);

  // Page backwards through history as the rider scrolls up
  const loadEarlier = async () => {
    if (!activeSessionId || loadingEarlier) return;
    const oldest = messages.find((m) => m.id);
    if (!oldest?.id) return;
    setLoadingEarlier(true);
    try {
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
        })),
        ...prev,
      ]);
      setHasEarlier(!!older.has_more);
    } finally {
      setLoadingEarlier(false);
    }
  };

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

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
    if (!input.trim() || streaming) return;

    let sessionId = activeSessionId;

    // Create session if none active
    if (!sessionId) {
      const session = await chat.createSession();
      sessionId = session.id;
      setActiveSessionId(session.id);
      queryClient.invalidateQueries({ queryKey: ["chat-sessions"] });
    }

    const userMessage = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: userMessage }]);
    setStreaming(true);

    // Add empty assistant message
    setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

    try {
      let assistantContent = "";
      for await (const chunk of chat.sendMessage(sessionId, userMessage)) {
        if (chunk.type === "text") {
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
          // Coach modified the training plan — refresh calendar data
          queryClient.invalidateQueries({ queryKey: ["workouts-week"] });
        }
      }
    } catch (error) {
      setMessages((prev) => {
        const updated = [...prev];
        updated[updated.length - 1] = {
          role: "assistant",
          content:
            "Sorry, I had trouble connecting. Please try again.",
        };
        return updated;
      });
    }

    setStreaming(false);
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
    <div className="flex h-[calc(100vh-5rem)] gap-0 md:h-[calc(100vh-3rem)] md:gap-4">
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
                    <span className="truncate">{session.title || "Chat"}</span>
                  </p>
                  <p className="text-[10px] text-vb-text-muted">
                    {session.archived_at
                      ? "Archived"
                      : `${session.message_count} messages`}
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
                            "Delete this chat for good? Forma keeps what it learned, but the conversation itself is gone."
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
            {sessions?.find((s) => s.id === activeSessionId)?.title || "Coach Forma"}
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
                  Training, racing, head, life, ask me anything. And I
                  remember what you tell me: every conversation sharpens{" "}
                  <Link href="/dashboard/brain" className="text-vb-forest hover:underline">
                    your brain
                  </Link>
                  , and better memory makes a better coach.
                </p>
                <StarterChips
                  starters={starters}
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
                {msg.role === "assistant" && (
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
                  <p className="f-kicker self-center text-vb-text-muted">
                    {coach} is thinking…
                  </p>
                ) : (
                  <div
                    className={cn(
                      "max-w-[90%] rounded-md px-3 py-2.5 text-sm sm:max-w-[80%] sm:px-4 sm:py-3",
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
                      <p>{msg.content}</p>
                    )}
                  </div>
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

          {/* Doors into the coach's range, context-aware and quiet */}
          {!input && (
            <StarterChips
              starters={starters}
              onPick={(ask) => {
                setInput(ask);
                inputRef.current?.focus();
              }}
              className="scrollbar-none mb-3 flex gap-2 overflow-x-auto"
            />
          )}

          <div className="flex items-end gap-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask your coach anything..."
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

            {/* Coach audio on/off — some riders want text only. Also stops
                playback mid-sentence when switched off. */}
            {voiceSupported && (
              <button
                onClick={toggleAudioOff}
                title={
                  audioOff
                    ? "Coach voice is off — click to hear Forma"
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
              disabled={!input.trim() || streaming}
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
