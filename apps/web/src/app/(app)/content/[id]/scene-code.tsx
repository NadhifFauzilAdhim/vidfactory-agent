"use client";

/**
 * Scene code viewer — shows the HTML/CSS/GSAP programme the LLM wrote for a scene (visual_mode =
 * "code"), so an operator can review what the renderer will actually run before approving.
 */

import { useState } from "react";
import { Braces, Loader2 } from "lucide-react";

type SceneCode = {
  scene_index: number;
  runtime: number;
  html: string;
  css: string;
  js: string;
  model: string | null;
  provider: string | null;
};

export function SceneCodeViewer({ codeUrl }: { codeUrl: string }) {
  const [code, setCode] = useState<SceneCode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function load() {
    if (code || pending) return;
    setPending(true);
    try {
      const path = codeUrl.startsWith("/api/v1/") ? codeUrl.slice("/api/v1".length) : codeUrl;
      const res = await fetch(`/api/proxy${path}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error?.message ?? `Request failed (${res.status})`);
      setCode(data as SceneCode);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load scene code");
    } finally {
      setPending(false);
    }
  }

  if (!code && !error) {
    return (
      <button
        type="button"
        onClick={load}
        disabled={pending}
        className="mt-2 inline-flex items-center gap-1.5 font-mono text-[9.5px] uppercase tracking-wider text-muted-foreground hover:text-ink"
      >
        {pending ? <Loader2 className="size-3 animate-spin" /> : <Braces className="size-3" />}
        scene code
      </button>
    );
  }

  if (error) {
    return <p className="mt-2 font-mono text-[10.5px] text-danger">{error}</p>;
  }

  return (
    <div className="mt-2 space-y-2">
      <p className="font-mono text-[9.5px] uppercase tracking-wider text-muted-foreground">
        scene code · runtime v{code?.runtime} · {code?.model ?? "unknown model"}
      </p>
      {(
        [
          ["html", code?.html],
          ["css", code?.css],
          ["gsap", code?.js],
        ] as const
      )
        .filter(([, value]) => value && value.trim().length > 0)
        .map(([label, value]) => (
          <details key={label} className="border-l border-border pl-2">
            <summary className="cursor-pointer font-mono text-[9.5px] uppercase tracking-wider text-muted-foreground hover:text-ink">
              {label} · {value!.length} chars
            </summary>
            <pre className="mt-1 max-h-56 overflow-auto bg-muted/40 p-2 font-mono text-[10px] leading-relaxed text-muted-foreground">
              {value}
            </pre>
          </details>
        ))}
    </div>
  );
}
