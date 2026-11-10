"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";

import { api, ApiError, type Diagnostics, type Me } from "@/lib/api";

interface AppContext {
  me: Me | undefined;
  diagnostics: Diagnostics | undefined;
  refreshDiagnostics: () => void;
  canReview: boolean;
  isAdmin: boolean;
}

const Ctx = createContext<AppContext>({
  me: undefined, diagnostics: undefined, refreshDiagnostics: () => undefined, canReview: false, isAdmin: false,
});

export function useApp(): AppContext {
  return useContext(Ctx);
}

export function AppProviders({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me>();
  const [diagnostics, setDiagnostics] = useState<Diagnostics>();
  const router = useRouter();
  const pathname = usePathname();

  const refreshDiagnostics = useCallback(() => {
    api.diagnostics().then(setDiagnostics).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (pathname === "/login") return;
    api.me()
      .then((user) => {
        setMe(user);
        refreshDiagnostics();
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
      });
  }, [pathname, router, refreshDiagnostics]);

  const canReview = me?.role === "admin" || me?.role === "reviewer";
  return (
    <Ctx.Provider value={{ me, diagnostics, refreshDiagnostics, canReview, isAdmin: me?.role === "admin" }}>
      {children}
    </Ctx.Provider>
  );
}
