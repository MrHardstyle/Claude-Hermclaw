/**
 * Reconnecting Server-Sent-Events client.
 *
 * - EventSource cannot send headers, the API accepts `?access_token=` on `/stream` endpoints.
 * - The browser's own reconnect sends `Last-Event-ID` automatically. When the connection is closed for good
 *   (HTTP error, proxy failure) we reconnect ourselves with exponential backoff and pass the last seen
 *   sequence as `?last_event_id=` so the server replays everything we missed.
 * - Events are delivered at most once (sequence-based de-duplication).
 */
import type { HermEvent } from "../api/types";
import { KNOWN_EVENT_TYPES, normalizeEvent } from "./events";

export type StreamState = "connecting" | "open" | "reconnecting" | "closed";

export interface EventSourceLike {
  readonly readyState: number;
  onopen: ((ev: Event) => unknown) | null;
  onerror: ((ev: Event) => unknown) | null;
  onmessage: ((ev: MessageEvent) => unknown) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface BackoffOptions {
  initialMs: number;
  maxMs: number;
  factor: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = { initialMs: 1000, maxMs: 30000, factor: 2 };

/** Delay before reconnect attempt `attempt` (0-based); deterministic so it can be unit tested. */
export function backoffDelay(attempt: number, opts: BackoffOptions = DEFAULT_BACKOFF): number {
  const raw = opts.initialMs * Math.pow(opts.factor, Math.max(0, attempt));
  return Math.min(opts.maxMs, Math.round(raw));
}

export function buildStreamUrl(base: string, path: string, token: string | null, lastEventId: number): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  const params = new URLSearchParams();
  if (token) params.set("access_token", token);
  if (lastEventId > 0) params.set("last_event_id", String(lastEventId));
  const qs = params.toString();
  return `${base}${p}${qs ? `?${qs}` : ""}`;
}

const CLOSED = 2;

export interface ReconnectingStreamOptions {
  /** builds the URL for a (re)connect given the last seen sequence */
  url: (lastEventId: number) => string;
  onEvent: (ev: HermEvent) => void;
  onState?: (state: StreamState, info: { attempt: number; nextRetryMs: number | null }) => void;
  initialLastEventId?: number;
  eventTypes?: readonly string[];
  factory?: EventSourceFactory;
  backoff?: BackoffOptions;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

export class ReconnectingEventStream {
  private es: EventSourceLike | null = null;
  private timer: unknown = null;
  private attempt = 0;
  private stopped = false;
  private _lastEventId: number;
  private _state: StreamState = "closed";
  private readonly opts: ReconnectingStreamOptions;
  private readonly factory: EventSourceFactory;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(opts: ReconnectingStreamOptions) {
    this.opts = opts;
    this._lastEventId = opts.initialLastEventId ?? 0;
    this.factory = opts.factory ?? ((url) => new EventSource(url) as unknown as EventSourceLike);
    this.setTimer = opts.setTimer ?? ((fn, ms) => window.setTimeout(fn, ms));
    this.clearTimer = opts.clearTimer ?? ((h) => { window.clearTimeout(h as number); });
  }

  get lastEventId(): number {
    return this._lastEventId;
  }

  get state(): StreamState {
    return this._state;
  }

  start(): void {
    this.stopped = false;
    this.connect("connecting");
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
    this.es?.close();
    this.es = null;
    this.setState("closed", null);
  }

  /** Feed an event obtained elsewhere (REST reconciliation) so the cursor never goes backwards. */
  advanceTo(sequence: number): void {
    if (sequence > this._lastEventId) this._lastEventId = sequence;
  }

  private setState(state: StreamState, nextRetryMs: number | null): void {
    this._state = state;
    this.opts.onState?.(state, { attempt: this.attempt, nextRetryMs });
  }

  private connect(state: StreamState): void {
    if (this.stopped) return;
    this.setState(state, null);
    const es = this.factory(this.opts.url(this._lastEventId));
    this.es = es;
    const handle = (msg: MessageEvent) => {
      if (this.es !== es) return;
      this.handleMessage(msg);
    };
    for (const t of this.opts.eventTypes ?? KNOWN_EVENT_TYPES) es.addEventListener(t, handle);
    es.onmessage = handle;
    es.onopen = () => {
      if (this.es !== es) return;
      this.attempt = 0;
      this.setState("open", null);
    };
    es.onerror = () => {
      if (this.es !== es || this.stopped) return;
      if (es.readyState === CLOSED) {
        es.close();
        this.es = null;
        this.scheduleReconnect();
      } else {
        // browser is retrying by itself (it sends Last-Event-ID)
        this.setState("reconnecting", null);
      }
    };
  }

  private scheduleReconnect(): void {
    const delay = backoffDelay(this.attempt, this.opts.backoff);
    this.attempt += 1;
    this.setState("reconnecting", delay);
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.connect("reconnecting");
    }, delay);
  }

  private handleMessage(msg: MessageEvent): void {
    let parsed: unknown;
    try {
      parsed = typeof msg.data === "string" ? JSON.parse(msg.data) : msg.data;
    } catch {
      return;
    }
    const ev = normalizeEvent(parsed);
    if (!ev) return;
    if (ev.sequence <= this._lastEventId) return; // duplicate after reconnect/replay
    this._lastEventId = ev.sequence;
    this.opts.onEvent(ev);
  }
}
