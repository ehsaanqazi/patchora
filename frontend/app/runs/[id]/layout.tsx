import type { ReactNode } from "react";

import { runs } from "@/lib/demo/data";

/** Static demo export: pre-render one page per demo run (also covers /runs/[id]/diff). */
export function generateStaticParams() {
  return process.env.NEXT_PUBLIC_DEMO_MODE === "true" ? runs.map((r) => ({ id: String(r.id) })) : [];
}

export default function RunLayout({ children }: { children: ReactNode }) {
  return children;
}
