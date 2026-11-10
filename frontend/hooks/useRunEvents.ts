"use client";

import { useEffect, useRef, useState } from "react";

import { DEMO_MODE } from "@/lib/api";

export interface RunEvent {
  id: number;
  event_type: string;
  message: string;
  data: Record<string, unknown>;
  created_at: string;
}

export type StreamStatus = "connecting" | "live" | "reconnecting" | "ended" | "error";

const MAX_BACKOFF_MS = 15_000;

/**
 * Subscribe to a run's Server-Sent Events. Reconnects with backoff, resuming from the last
 * received event id (`?after=`), and stops once the server sends `end` (run finished or
 * parked waiting for approval). `onEvent` is called for every new event.
 */
export function useRunEvents(runId: number, onEvent?: (event: RunEvent) => void, enabled = true) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const lastId = useRef(0);
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  });

  useEffect(() => {
    if (!enabled || !DEMO_MODE) return;
    // Demo build: no backend stream; replay the run's stored events once.
    let cancelled = false;
    import("@/lib/demo/handler").then(({ demoRunEvents }) => {
      if (cancelled) return;
      const replay = demoRunEvents(runId);
      lastId.current = replay.at(-1)?.id ?? 0;
      setEvents(replay);
      setStatus("ended");
    });
    return () => { cancelled = true; };
  }, [runId, enabled]);

  useEffect(() => {
    if (!enabled || DEMO_MODE) return;
    let source: EventSource | null = null;
    let closed = false;
    let attempt = 0;
    let timer: number | undefined;

    const connect = () => {
      if (closed) return;
      setStatus(attempt === 0 ? "connecting" : "reconnecting");
      source = new EventSource(`/api/v1/runs/${runId}/events?after=${lastId.current}`);
      source.onopen = () => {
        attempt = 0;
        setStatus("live");
      };
      const handle = (message: MessageEvent<string>) => {
        try {
          const event = JSON.parse(message.data) as RunEvent;
          if (event.id <= lastId.current) return;
          lastId.current = event.id;
          setEvents((prev) => [...prev, event]);
          onEventRef.current?.(event);
        } catch {
          // ignore malformed frames
        }
      };
      source.onmessage = handle;
      // Named events: subscribe generically by intercepting all types we emit.
      for (const type of EVENT_TYPES) source.addEventListener(type, handle as EventListener);
      source.addEventListener("end", () => {
        closed = true;
        setStatus("ended");
        source?.close();
      });
      source.onerror = () => {
        source?.close();
        if (closed) return;
        attempt += 1;
        setStatus("reconnecting");
        const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(attempt, 4));
        timer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      source?.close();
    };
  }, [runId, enabled]);

  return { events, status };
}

const EVENT_TYPES = [
  "run_queued", "run_started", "sentry_issue_retrieved", "repository_resolved", "source_inspected",
  "evidence_expanded", "diagnosis_generated", "patch_planned", "patch_generated", "patch_reviewed",
  "patch_validation_failed", "retry_started", "retry_scheduled", "step_failed", "tests_started",
  "tests_completed", "approval_required", "approval_granted", "approval_rejected", "ready_to_publish",
  "branch_published", "pull_request_created", "github_issue_created", "run_completed", "run_failed",
  "run_cancelled", "cancel_requested", "needs_human_review", "run_recovered", "state_changed",
];
