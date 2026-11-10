import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { AppProviders } from "@/components/AppProviders";
import { Shell } from "@/components/Shell";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Patchora", template: "%s · Patchora" },
  description: "Sentry-to-GitHub bug investigation, repair, validation and pull requests with human review.",
};

export const viewport: Viewport = { themeColor: "#0b0e14" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AppProviders>
          <Shell>{children}</Shell>
        </AppProviders>
      </body>
    </html>
  );
}
