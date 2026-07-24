"use client";

import { useState, useRef, useCallback, useEffect } from "react";

// Extend Window interface for webkit prefix
interface SpeechRecognitionEvent extends Event {
  results: SpeechRecognitionResultList;
  resultIndex: number;
}

interface SpeechRecognitionErrorEvent extends Event {
  error: string;
  message?: string;
}

interface SpeechRecognitionInstance extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: SpeechRecognitionEvent) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
}

declare global {
  interface Window {
    webkitSpeechRecognition: new () => SpeechRecognitionInstance;
    SpeechRecognition: new () => SpeechRecognitionInstance;
  }
}

interface UseVoiceChatOptions {
  /** Called with the final transcript when the user stops speaking */
  onTranscript: (text: string) => void;
  /** Language for speech recognition (default: en-US) */
  lang?: string;
}

interface UseVoiceChatReturn {
  /** Whether the microphone is actively listening */
  isListening: boolean;
  /** Whether TTS audio is currently playing */
  isSpeaking: boolean;
  /** Whether the browser supports Web Speech API */
  isSupported: boolean;
  /** Real-time transcript while user is speaking */
  interimTranscript: string;
  /** Start listening for speech input */
  startListening: () => void;
  /** Stop listening for speech input */
  stopListening: () => void;
  /** Queue a base64-encoded audio chunk for playback */
  playAudioChunk: (base64Audio: string) => void;
  /** Stop all audio playback and clear the queue */
  stopAudio: () => void;
}

/**
 * Custom hook for voice chat functionality.
 *
 * Combines:
 * - Web Speech API for speech-to-text (STT)
 * - Audio queue for sequential playback of TTS chunks
 *
 * Browser support: Chrome, Edge, Safari 14.1+
 * Not supported: Firefox
 */
export function useVoiceChat({
  onTranscript,
  lang = "en-US",
}: UseVoiceChatOptions): UseVoiceChatReturn {
  const [isListening, setIsListening] = useState(false);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [interimTranscript, setInterimTranscript] = useState("");
  const [isSupported, setIsSupported] = useState(false);

  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);
  const audioQueueRef = useRef<string[]>([]);
  const isPlayingRef = useRef(false);
  const currentAudioRef = useRef<HTMLAudioElement | null>(null);
  const onTranscriptRef = useRef(onTranscript);

  // Keep callback ref fresh
  useEffect(() => {
    onTranscriptRef.current = onTranscript;
  }, [onTranscript]);

  // Check browser support on mount
  useEffect(() => {
    const supported =
      typeof window !== "undefined" &&
      ("webkitSpeechRecognition" in window ||
        "SpeechRecognition" in window);
    setIsSupported(supported);
  }, []);

  // Process the audio queue — play chunks one at a time
  const processQueue = useCallback(async () => {
    if (isPlayingRef.current) return;
    if (audioQueueRef.current.length === 0) {
      setIsSpeaking(false);
      return;
    }

    isPlayingRef.current = true;
    setIsSpeaking(true);

    while (audioQueueRef.current.length > 0) {
      const base64Audio = audioQueueRef.current.shift()!;

      try {
        // Decode base64 to binary
        const binaryString = atob(base64Audio);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
          bytes[i] = binaryString.charCodeAt(i);
        }

        const blob = new Blob([bytes], { type: "audio/mpeg" });
        const url = URL.createObjectURL(blob);

        await new Promise<void>((resolve, reject) => {
          const audio = new Audio(url);
          currentAudioRef.current = audio;

          audio.onended = () => {
            URL.revokeObjectURL(url);
            currentAudioRef.current = null;
            resolve();
          };

          audio.onerror = () => {
            URL.revokeObjectURL(url);
            currentAudioRef.current = null;
            reject(new Error("Audio playback failed"));
          };

          audio.play().catch((err) => {
            URL.revokeObjectURL(url);
            currentAudioRef.current = null;
            reject(err);
          });
        });
      } catch {
        // Skip failed audio chunks, continue with queue
        continue;
      }
    }

    isPlayingRef.current = false;
    setIsSpeaking(false);
  }, []);

  // Queue a base64 audio chunk and start processing
  const playAudioChunk = useCallback(
    (base64Audio: string) => {
      audioQueueRef.current.push(base64Audio);
      processQueue();
    },
    [processQueue]
  );

  // Stop all audio playback
  const stopAudio = useCallback(() => {
    audioQueueRef.current = [];
    if (currentAudioRef.current) {
      currentAudioRef.current.pause();
      currentAudioRef.current = null;
    }
    isPlayingRef.current = false;
    setIsSpeaking(false);
  }, []);

  // Start speech recognition
  const startListening = useCallback(() => {
    if (!isSupported) return;

    // Stop any existing recognition
    if (recognitionRef.current) {
      recognitionRef.current.abort();
    }

    const SpeechRecognition =
      window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new SpeechRecognition();

    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = lang;

    // A thinking pause is not the end of a thought. The browser finalises a
    // speech segment after well under a second of silence, so we buffer the
    // finalised text and only send after a real grace window with no new
    // speech. Talking again during the window resets it and appends.
    const SILENCE_GRACE_MS = 2000;
    let finalBuffer = "";
    let sendTimer: number | null = null;

    const flushBuffer = () => {
      if (sendTimer !== null) {
        window.clearTimeout(sendTimer);
        sendTimer = null;
      }
      const text = finalBuffer.trim();
      finalBuffer = "";
      if (text) {
        setInterimTranscript("");
        recognition.stop();
        setIsListening(false);
        onTranscriptRef.current(text);
      }
    };

    const armSendTimer = () => {
      if (sendTimer !== null) window.clearTimeout(sendTimer);
      sendTimer = window.setTimeout(flushBuffer, SILENCE_GRACE_MS);
    };

    recognition.onstart = () => {
      setIsListening(true);
      setInterimTranscript("");
    };

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      let interim = "";
      let finalTranscript = "";

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          finalTranscript += result[0].transcript;
        } else {
          interim += result[0].transcript;
        }
      }

      if (finalTranscript.trim()) {
        finalBuffer += (finalBuffer ? " " : "") + finalTranscript.trim();
      }
      // Show what's buffered + what's in flight, so the rider sees the
      // whole thought building, not fragments vanishing.
      setInterimTranscript(
        [finalBuffer, interim.trim()].filter(Boolean).join(" ")
      );

      // Any speech (interim or final) resets the grace window.
      if (finalBuffer || interim.trim()) armSendTimer();
    };

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      // "no-speech" and "aborted" are normal — user didn't speak or we stopped
      if (event.error !== "no-speech" && event.error !== "aborted") {
        console.warn("Speech recognition error:", event.error);
      }
      // Don't lose a buffered thought to a recognition hiccup.
      flushBuffer();
      setIsListening(false);
      setInterimTranscript("");
    };

    recognition.onend = () => {
      // Browser can end recognition on its own after long silence —
      // send whatever was said rather than dropping it.
      flushBuffer();
      setIsListening(false);
    };

    recognitionRef.current = recognition;

    try {
      recognition.start();
    } catch {
      // Already started or not available
      setIsListening(false);
    }
  }, [isSupported, lang]);

  // Stop speech recognition
  const stopListening = useCallback(() => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
    }
    setIsListening(false);
    setInterimTranscript("");
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (recognitionRef.current) {
        recognitionRef.current.abort();
      }
      stopAudio();
    };
  }, [stopAudio]);

  return {
    isListening,
    isSpeaking,
    isSupported,
    interimTranscript,
    startListening,
    stopListening,
    playAudioChunk,
    stopAudio,
  };
}
