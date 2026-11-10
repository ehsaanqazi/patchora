"use client";

import { ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ThemeToggle } from "@/components/ThemeToggle";
import { Button, ErrorState, Field, Input } from "@/components/ui";
import { useAction } from "@/hooks/useApi";
import { api } from "@/lib/api";

/** Token sign-in (AUTH_MODE=token). The token is exchanged once for an HttpOnly session cookie. */
export default function LoginPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const login = useAction(async () => {
    await api.login(token.trim());
    setToken("");
    router.replace("/");
  });
  return (
    <main className="relative grid min-h-dvh place-items-center px-4">
      <ThemeToggle className="absolute right-4 top-4" />
      <form className="w-full max-w-sm space-y-5 rounded-xl border border-line bg-surface p-6" autoComplete="off"
        onSubmit={(e) => { e.preventDefault(); void login.run(); }}>
        <div className="flex items-center gap-3">
          <span className="grid size-10 place-items-center rounded-lg bg-accent-soft text-accent-strong"><ShieldCheck className="size-5" aria-hidden /></span>
          <div>
            <h1 className="text-base font-semibold">Sign in to Patchora</h1>
            <p className="text-xs text-ink-muted">Use the API token issued by an administrator.</p>
          </div>
        </div>
        <Field label="API token" hint="The token is sent once and replaced by an HttpOnly session cookie.">
          {(id) => <Input id={id} type="password" required minLength={10} value={token} onChange={(e) => setToken(e.target.value)} />}
        </Field>
        {login.error && <ErrorState error={login.error} />}
        <Button type="submit" variant="primary" className="w-full" loading={login.pending}>Sign in</Button>
      </form>
    </main>
  );
}
