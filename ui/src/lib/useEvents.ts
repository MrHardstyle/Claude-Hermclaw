import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { HermEvent } from "../api/types";
import { getToken } from "./auth";
import { API_BASE } from "./config";
import { KNOWN_EVENT_TYPES, maxSequence, mergeEvents } from "./events";
import { buildStreamUrl, ReconnectingEventStream, undeliveredSequence, type StreamState } from "./sse";

export interface StreamInfo {
  state: StreamState;
  attempt: number;
  nextRetryMs: number | null;
}

export interface JobEventsResult {
  events: HermEvent[];
  loaded: boolean;
  error: unknown;
  stream: StreamInfo;
  lastSequence: number;
}

const PAGE = 5000;
/** Re-read this many sequence numbers below the cursor on reconcile: events committed out of order are not lost. */
export const RECONCILE_WINDOW = 500;
export const RECONCILE_INTERVAL_MS = 15000;
/** A listened-for event REST already returns must arrive on an open stream within this time, else it is stale. */
export const STALE_GRACE_MS = 5000;
const LISTENED: ReadonlySet<string> = new Set(KNOWN_EVENT_TYPES);

/**
 * Job events: full REST history, then live SSE (`/jobs/{id}/events/stream`) with auto-reconnect and Last-Event-ID,
 * plus a periodic REST reconciliation. The reconciliation also picks up event types the EventSource has no
 * listener for (EventSource has no wildcard) and events whose transaction committed after a higher sequence.
 * It doubles as watchdog: when REST returns an event the open stream should have delivered and the stream still
 * has not after STALE_GRACE_MS, the connection is half-open (dead upstream behind a proxy) and is replaced.
 */
export function useJobEvents(jobId: string): JobEventsResult {
  const [events, setEvents] = useState<HermEvent[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<unknown>(undefined);
  const [stream, setStream] = useState<StreamInfo>({ state: "connecting", attempt: 0, nextRetryMs: null });

  useEffect(() => {
    let cancelled = false;
    // a function (not a narrowed boolean) so checks after `await` stay meaningful
    const alive = () => !cancelled;
    const isAbort = (e: unknown) => e instanceof DOMException && e.name === "AbortError";
    const ctrl = new AbortController();
    let es: ReconnectingEventStream | null = null;
    let cursor = 0;
    let reconciling = false;
    let wasOpen = false;
    let staleTimer: number | null = null;

    const add = (batch: readonly HermEvent[]) => {
      if (!alive() || batch.length === 0) return;
      const own = batch.filter((e) => e.job_id === null || e.job_id === jobId);
      if (!own.length) return;
      cursor = Math.max(cursor, maxSequence(own));
      es?.advanceTo(cursor);
      setEvents((prev) => mergeEvents(prev, own));
    };

    const loadFrom = async (after: number): Promise<HermEvent[]> => {
      let from = after;
      const all: HermEvent[] = [];
      for (;;) {
        const batch = await api.events(jobId, { after: from, limit: PAGE }, ctrl.signal);
        add(batch);
        all.push(...batch);
        if (batch.length < PAGE) break;
        from = maxSequence(batch);
      }
      return all;
    };

    const checkStale = (fetched: readonly HermEvent[]) => {
      const current = es;
      if (!current || current.state !== "open" || staleTimer !== null) return;
      const missing = undeliveredSequence(fetched, current.seen, LISTENED);
      if (missing === null) return;
      staleTimer = window.setTimeout(() => {
        staleTimer = null;
        if (!alive() || es !== current || current.state !== "open") return;
        if (current.seen < missing) current.reconnectNow();
      }, STALE_GRACE_MS);
    };

    const reconcile = async () => {
      if (reconciling || !alive()) return;
      reconciling = true;
      try {
        checkStale(await loadFrom(Math.max(0, cursor - RECONCILE_WINDOW)));
        if (alive()) setError(undefined);
      } catch (e) {
        if (alive() && !isAbort(e)) setError(e);
      } finally {
        reconciling = false;
      }
    };

    const startStream = () => {
      es = new ReconnectingEventStream({
        url: (last) => buildStreamUrl(API_BASE, `/jobs/${encodeURIComponent(jobId)}/events/stream`, getToken(), last),
        initialLastEventId: cursor,
        onEvent: (ev) => {
          add([ev]);
        },
        onState: (state, info) => {
          if (!alive()) return;
          setStream({ state, attempt: info.attempt, nextRetryMs: info.nextRetryMs });
          if (state === "open") {
            // after a reconnect: catch up on whatever the replay could not deliver
            if (wasOpen) void reconcile();
            wasOpen = true;
          }
        },
      });
      es.start();
    };

    loadFrom(0).then(
      () => {
        if (!alive()) return;
        setLoaded(true);
        setError(undefined);
        startStream();
      },
      (e: unknown) => {
        if (!alive() || isAbort(e)) return;
        setError(e);
        setLoaded(true);
        startStream();
      },
    );

    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void reconcile();
    }, RECONCILE_INTERVAL_MS);

    return () => {
      cancelled = true;
      ctrl.abort();
      window.clearInterval(timer);
      if (staleTimer !== null) window.clearTimeout(staleTimer);
      es?.stop();
    };
  }, [jobId]);

  return { events, loaded, error, stream, lastSequence: maxSequence(events) };
}

/**
 * Global live events (`/events/stream`, starts "now"): keeps the newest `max` events in memory.
 * Used by the dashboard for recent errors and the live status ticker.
 */
export function useGlobalEvents(max = 300): { events: HermEvent[]; stream: StreamInfo } {
  const [events, setEvents] = useState<HermEvent[]>([]);
  const [stream, setStream] = useState<StreamInfo>({ state: "connecting", attempt: 0, nextRetryMs: null });
  const maxRef = useRef(max);
  useEffect(() => {
    maxRef.current = max;
  }, [max]);

  useEffect(() => {
    let active = true;
    const es = new ReconnectingEventStream({
      url: (last) => buildStreamUrl(API_BASE, "/events/stream", getToken(), last),
      onEvent: (ev) => {
        if (active) setEvents((prev) => mergeEvents(prev, [ev], maxRef.current));
      },
      onState: (state, info) => {
        if (active) setStream({ state, attempt: info.attempt, nextRetryMs: info.nextRetryMs });
      },
    });
    es.start();
    return () => {
      active = false;
      es.stop();
    };
  }, []);

  return { events, stream };
}

export const STREAM_STATE_LABELS: Record<StreamState, string> = {
  connecting: "Verbinde …",
  open: "Live verbunden",
  reconnecting: "Verbindung unterbrochen – verbinde neu",
  closed: "Getrennt",
};
