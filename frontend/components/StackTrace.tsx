"use client";

import clsx from "clsx";
import { useState } from "react";

import { Badge } from "@/components/ui";
import type { NormalizedEvent } from "@/lib/api";

/** Renders exceptions innermost-last as Sentry does; library frames are collapsed by default. */
export function StackTrace({ event }: { event: NormalizedEvent }) {
  const [showAll, setShowAll] = useState(false);
  if (!event.exceptions?.length) {
    return <p className="text-sm text-ink-muted">This event has no stack trace. Evidence is limited to the error message.</p>;
  }
  return (
    <div className="space-y-5">
      {event.exceptions.map((exc, index) => {
        const frames = exc.frames ?? [];
        const visible = showAll ? frames : frames.filter((f) => f.in_app !== false);
        return (
          <div key={index}>
            <p className="font-mono text-sm">
              <span className="font-semibold text-bad">{exc.type ?? "Exception"}</span>
              {exc.value && <span className="text-ink">: {exc.value}</span>}
            </p>
            {exc.mechanism && <p className="mt-0.5 text-xs text-ink-faint">mechanism: {exc.mechanism}</p>}
            <ol className="mt-3 overflow-hidden rounded-lg border border-line" aria-label="Stack frames (most recent call last)">
              {visible.map((frame, i) => {
                const contextLine = frame.context?.find(([line]) => line === frame.lineno)?.[1];
                return (
                  <li key={i} className={clsx("border-b border-line/60 px-3 py-2 last:border-b-0",
                    frame.in_app ? "bg-surface-2" : "bg-surface")}>
                    <div className="flex flex-wrap items-center gap-2 font-mono text-xs">
                      <span className="text-ink">{frame.filename ?? frame.abs_path ?? "<unknown>"}</span>
                      {frame.lineno != null && <span className="text-ink-faint">line {frame.lineno}</span>}
                      <span className="text-ink-faint">in</span>
                      <span className="text-info">{frame.function ?? "?"}</span>
                      {frame.in_app ? <Badge tone="accent">app</Badge> : <Badge>library</Badge>}
                    </div>
                    {contextLine && (
                      <pre className="mt-1.5 overflow-x-auto rounded bg-canvas px-2 py-1 font-mono text-xs text-ink-muted">
                        {contextLine}
                      </pre>
                    )}
                  </li>
                );
              })}
              {visible.length === 0 && <li className="px-3 py-2 text-xs text-ink-faint">No application frames.</li>}
            </ol>
          </div>
        );
      })}
      <button type="button" className="text-xs text-accent-strong hover:underline" onClick={() => setShowAll(!showAll)}>
        {showAll ? "Hide library frames" : "Show library frames"}
      </button>
    </div>
  );
}
