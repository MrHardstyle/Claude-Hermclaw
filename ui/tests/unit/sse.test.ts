import { describe, expect, it } from "vitest";
import { backoffDelay, buildStreamUrl, ReconnectingEventStream, type EventSourceLike } from "../../src/lib/sse";
import type { HermEvent } from "../../src/api/types";

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  readyState = 0;
  onopen: ((ev: Event) => unknown) | null = null;
  onerror: ((ev: Event) => unknown) | null = null;
  onmessage: ((ev: MessageEvent) => unknown) | null = null;
  listeners = new Map<string, ((ev: MessageEvent) => void)[]>();
  closed = false;
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, l: (ev: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), l]);
  }
  close(): void {
    this.closed = true;
    this.readyState = 2;
  }
  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  emit(type: string, data: unknown): void {
    const ev = { data: JSON.stringify(data) } as MessageEvent;
    for (const l of this.listeners.get(type) ?? []) l(ev);
  }
  fail(permanent: boolean): void {
    this.readyState = permanent ? 2 : 0;
    this.onerror?.(new Event("error"));
  }
}

function envelope(sequence: number, type = "status", text = "x") {
  return { event_id: `e${sequence}`, sequence, timestamp: "2026-10-08T10:00:00Z", source_type: "runtime", event_type: type, severity: "info", payload: { text } };
}

function setup(initial = 0) {
  FakeEventSource.instances = [];
  const timers: { fn: () => void; ms: number }[] = [];
  const received: HermEvent[] = [];
  const states: string[] = [];
  const stream = new ReconnectingEventStream({
    url: (last) => buildStreamUrl("/api", "/jobs/j1/events/stream", "tok en", last),
    onEvent: (e) => received.push(e),
    onState: (s) => states.push(s),
    initialLastEventId: initial,
    factory: (u) => new FakeEventSource(u),
    setTimer: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimer: () => undefined,
  });
  return { stream, timers, received, states };
}

describe("backoffDelay", () => {
  it("grows exponentially and is capped", () => {
    expect([0, 1, 2, 3, 4, 5, 6].map((a) => backoffDelay(a))).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    expect(backoffDelay(2, { initialMs: 100, maxMs: 250, factor: 3 })).toBe(250);
  });
});

describe("buildStreamUrl", () => {
  it("adds access token and last event id", () => {
    expect(buildStreamUrl("/api", "/events/stream", "a b", 42)).toBe("/api/events/stream?access_token=a+b&last_event_id=42");
    expect(buildStreamUrl("/api", "events/stream", null, 0)).toBe("/api/events/stream");
  });
});

describe("ReconnectingEventStream", () => {
  it("delivers named events and de-duplicates by sequence", () => {
    const { stream, received, states } = setup(5);
    stream.start();
    const es = FakeEventSource.instances[0];
    expect(es?.url).toBe("/api/jobs/j1/events/stream?access_token=tok+en&last_event_id=5");
    es?.open();
    es?.emit("status", envelope(4)); // older than cursor
    es?.emit("status", envelope(6, "status", "Gemma erstellt Plan"));
    es?.emit("job.transition", envelope(7, "job.transition"));
    es?.emit("status", envelope(7)); // duplicate
    expect(received.map((e) => e.sequence)).toEqual([6, 7]);
    expect(received[0]?.payload.text).toBe("Gemma erstellt Plan");
    expect(received[0]?.ts).toBe("2026-10-08T10:00:00Z");
    expect(stream.lastEventId).toBe(7);
    expect(states).toEqual(["connecting", "open"]);
  });

  it("reconnects with backoff and the last seen id after a permanent failure", () => {
    const { stream, timers, received, states } = setup();
    stream.start();
    const first = FakeEventSource.instances[0];
    first?.open();
    first?.emit("status", envelope(10));
    first?.fail(true);
    expect(first?.closed).toBe(true);
    expect(timers.map((t) => t.ms)).toEqual([1000]);
    timers[0]?.fn();
    const second = FakeEventSource.instances[1];
    expect(second?.url).toContain("last_event_id=10");
    second?.fail(true);
    expect(timers.map((t) => t.ms)).toEqual([1000, 2000]);
    timers[1]?.fn();
    const third = FakeEventSource.instances[2];
    third?.open();
    third?.emit("status", envelope(10)); // replayed duplicate is dropped
    third?.emit("status", envelope(11));
    expect(received.map((e) => e.sequence)).toEqual([10, 11]);
    third?.fail(true);
    expect(timers[2]?.ms).toBe(1000); // backoff reset after successful open
    expect(states).toContain("reconnecting");
  });

  it("lets the browser retry transient errors itself", () => {
    const { stream, timers, states } = setup();
    stream.start();
    FakeEventSource.instances[0]?.fail(false);
    expect(timers).toHaveLength(0);
    expect(states.at(-1)).toBe("reconnecting");
  });

  it("ignores events from a stale connection and stops cleanly", () => {
    const { stream, received, states } = setup();
    stream.start();
    const first = FakeEventSource.instances[0];
    stream.stop();
    first?.emit("status", envelope(1));
    expect(received).toEqual([]);
    expect(first?.closed).toBe(true);
    expect(states.at(-1)).toBe("closed");
  });

  it("advances the cursor from REST reconciliation", () => {
    const { stream, received } = setup();
    stream.start();
    stream.advanceTo(20);
    FakeEventSource.instances[0]?.emit("status", envelope(15));
    expect(received).toEqual([]);
    expect(stream.lastEventId).toBe(20);
  });

  it("drops malformed payloads", () => {
    const { stream, received } = setup();
    stream.start();
    const es = FakeEventSource.instances[0];
    es?.listeners.get("status")?.[0]?.({ data: "not json" } as MessageEvent);
    es?.emit("status", { foo: 1 });
    expect(received).toEqual([]);
  });
});
