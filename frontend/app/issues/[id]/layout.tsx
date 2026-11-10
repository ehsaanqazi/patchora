import type { ReactNode } from "react";

import { issues } from "@/lib/demo/data";

/** Static demo export: pre-render one page per demo issue. */
export function generateStaticParams() {
  return process.env.NEXT_PUBLIC_DEMO_MODE === "true" ? issues.map((i) => ({ id: String(i.id) })) : [];
}

export default function IssueLayout({ children }: { children: ReactNode }) {
  return children;
}
