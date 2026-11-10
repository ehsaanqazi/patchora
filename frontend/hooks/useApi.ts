"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";

export interface ApiState<T> {
  data: T | undefined;
  error: ApiError | Error | undefined;
  loading: boolean;
  reload: () => void;
}

interface Loaded<T> { key?: string; data?: T; error?: Error }

/**
 * Fetch data on mount and whenever `deps` (primitive values) change; optional polling in ms.
 * Previous data stays visible while a new request is in flight (`loading` is true until the
 * response for the current deps arrives).
 */
export function useApi<T>(loader: () => Promise<T>, deps: unknown[] = [], pollMs?: number): ApiState<T> {
  const [tick, setTick] = useState(0);
  const [state, setState] = useState<Loaded<T>>({});
  const loaderRef = useRef(loader);
  useEffect(() => {
    loaderRef.current = loader;
  });
  const key = JSON.stringify([deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    loaderRef
      .current()
      .then((data) => { if (!cancelled) setState({ key, data }); })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState((prev) => ({ key, data: prev.data, error: err instanceof Error ? err : new Error(String(err)) }));
        }
      });
    return () => { cancelled = true; };
  }, [key]);

  useEffect(() => {
    if (!pollMs) return;
    const id = window.setInterval(() => {
      loaderRef
        .current()
        .then((data) => setState((prev) => ({ key: prev.key, data })))
        .catch(() => undefined); // keep showing the last good data while polling
    }, pollMs);
    return () => window.clearInterval(id);
  }, [pollMs]);

  return { data: state.data, error: state.error, loading: state.key !== key, reload };
}

/** Run a mutation with pending/error state. */
export function useAction<A extends unknown[], R>(action: (...args: A) => Promise<R>) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error>();
  const actionRef = useRef(action);
  useEffect(() => {
    actionRef.current = action;
  });
  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    setPending(true);
    setError(undefined);
    try {
      return await actionRef.current(...args);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
      return undefined;
    } finally {
      setPending(false);
    }
  }, []);
  const clearError = useCallback(() => setError(undefined), []);
  return { run, pending, error, clearError };
}
