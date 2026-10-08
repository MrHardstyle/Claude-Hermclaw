import { useCallback, useEffect, useRef, useState } from "react";

export interface ApiResult<T> {
  data: T | undefined;
  error: unknown;
  /** true until the first response for the current key arrived */
  loading: boolean;
  /** true while a refresh (reload/poll/version change) is in flight */
  refreshing: boolean;
  updatedAt: number | null;
  reload: () => void;
}

interface State<T> {
  key: string | null;
  data: T | undefined;
  error: unknown;
  doneReq: string; // request id that produced this state
  updatedAt: number | null;
}

export interface UseApiOptions {
  /** poll interval in ms (skipped while the tab is hidden) */
  pollMs?: number;
  /** changing the version refetches but keeps the current data visible */
  version?: string | number;
}

/**
 * Fetch hook keyed by a string: `key === null` disables the request; a new key drops old data,
 * `reload()`, polling and `version` refresh in place. Aborts in-flight requests on change/unmount.
 */
export function useApi<T>(key: string | null, fetcher: (signal: AbortSignal) => Promise<T>, opts: UseApiOptions = {}): ApiResult<T> {
  const { pollMs, version } = opts;
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<State<T>>({ key: null, data: undefined, error: undefined, doneReq: "", updatedAt: null });
  const reqId = `${key ?? ""}|${String(version ?? "")}|${tick}`;
  const [started, setStarted] = useState<string>("");

  useEffect(() => {
    if (key === null) return;
    const ctrl = new AbortController();
    let active = true;
    const run = Promise.resolve().then(() => {
      if (active) setStarted(reqId);
      return fetcherRef.current(ctrl.signal);
    });
    run.then(
      (data) => {
        if (!active) return;
        setState({ key, data, error: undefined, doneReq: reqId, updatedAt: Date.now() });
      },
      (error: unknown) => {
        if (!active || (error instanceof DOMException && error.name === "AbortError")) return;
        setState((s) => ({ key, data: s.key === key ? s.data : undefined, error, doneReq: reqId, updatedAt: Date.now() }));
      },
    );
    return () => {
      active = false;
      ctrl.abort();
    };
    // reqId encodes key, version and tick
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reqId]);

  useEffect(() => {
    if (!pollMs || key === null) return;
    const id = window.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      setTick((t) => t + 1);
    }, pollMs);
    return () => {
      window.clearInterval(id);
    };
  }, [key, pollMs]);

  const reload = useCallback(() => {
    setTick((t) => t + 1);
  }, []);
  const sameKey = state.key === key;
  return {
    data: sameKey ? state.data : undefined,
    error: sameKey ? state.error : undefined,
    loading: key !== null && !sameKey,
    refreshing: key !== null && started === reqId && state.doneReq !== reqId,
    updatedAt: sameKey ? state.updatedAt : null,
    reload,
  };
}

/** Current time, refreshed every `intervalMs` (keeps render pure for relative timestamps). */
export function useNow(intervalMs = 5000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      window.clearInterval(id);
    };
  }, [intervalMs]);
  return now;
}
