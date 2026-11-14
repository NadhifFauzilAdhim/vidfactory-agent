"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Save, Settings2 } from "lucide-react";
import { toast } from "sonner";

import type { Agent, Provider } from "@/lib/types";
import { buttonStyles } from "@/components/ui/button";
import { Input, Select, Textarea } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export function AgentConfigurator({ agent, providers }: { agent: Agent; providers: Provider[] }) {
  const router = useRouter();
  const eligible = providers.filter((p) => p.kind === agent.port);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [form, setForm] = useState({
    provider_id: agent.provider_id ?? "",
    model: agent.model ?? "",
    temperature: agent.temperature,
    max_tokens: agent.max_tokens,
    system_prompt: agent.system_prompt ?? "",
    is_enabled: agent.is_enabled,
  });

  const provider = eligible.find((p) => p.id === form.provider_id);
  const needsProvider = ["llm", "tts", "image", "youtube"].includes(agent.port);
  // Only an agent that calls a language model has a token budget. Image/tts/video/rules agents
  // have no token parameter on their provider, so their fields stay read-only.
  const canTuneGeneration = agent.port === "llm";

  async function save() {
    setPending(true);
    try {
      const res = await fetch(`/api/proxy/agents/${agent.key}/config`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          provider_id: form.provider_id || null,
          model: form.model || null,
          temperature: Number(form.temperature),
          max_tokens: Number(form.max_tokens),
          system_prompt: form.system_prompt || null,
          is_enabled: form.is_enabled,
          params: agent.params ?? {},
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error?.message ?? "Save failed");
      toast.success(`${agent.name} updated`);
      setOpen(false);
      router.refresh();
    } catch (err) {
      toast.error("Configuration rejected", { description: (err as Error).message });
    } finally {
      setPending(false);
    }
  }

  return (
    <div>
      <button
        type="button"
        className={cn(buttonStyles("outline", "xs"), open && "bg-ink text-white hover:bg-ink hover:text-white")}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Settings2 className="!size-2.5" /> configure
      </button>
      {open && (
        <div className="mt-3 border border-ink bg-paper p-3 animate-slide-up">
          {needsProvider ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="mono-label mb-1 block text-ink">provider · {agent.port}</span>
                <Select value={form.provider_id} onChange={(e) => setForm({ ...form, provider_id: e.target.value, model: "" })}>
                  <option value="">auto (first enabled)</option>
                  {eligible.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}{p.is_mock ? " · mock" : p.has_credential ? " · ready" : " · no key"}
                    </option>
                  ))}
                </Select>
              </label>
              <label className="block">
                <span className="mono-label mb-1 block text-ink">model</span>
                <Select value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })}>
                  <option value="">provider default</option>
                  {(provider?.models ?? []).map((m) => (
                    <option key={m.id} value={m.id}>{m.name ?? m.id}</option>
                  ))}
                </Select>
              </label>
            </div>
          ) : (
            <p className="mb-3 border-l-2 border-border pl-2 text-[12px] text-muted-foreground">
              This agent runs deterministic {agent.port === "video" ? "FFmpeg rendering" : "checks"} — no model
              provider is required.
            </p>
          )}

          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <label className="block">
              <span className="mono-label mb-1 block text-ink">temperature</span>
              <Input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={form.temperature}
                disabled={!canTuneGeneration}
                onChange={(e) => setForm({ ...form, temperature: Number(e.target.value) })}
              />
            </label>
            <label className="block">
              <span className="mono-label mb-1 block text-ink">max tokens</span>
              <Input
                type="number"
                min={256}
                max={128000}
                value={form.max_tokens}
                disabled={!canTuneGeneration}
                onChange={(e) => setForm({ ...form, max_tokens: Number(e.target.value) })}
              />
            </label>
            <label className="block">
              <span className="mono-label mb-1 block text-ink">agent enabled</span>
              <Select
                value={form.is_enabled ? "on" : "off"}
                onChange={(e) => setForm({ ...form, is_enabled: e.target.value === "on" })}
              >
                <option value="on">enabled</option>
                <option value="off">paused</option>
              </Select>
            </label>
          </div>

          <label className="mt-3 block">
            <span className="mono-label mb-1 flex items-center justify-between text-ink">
              <span>system prompt</span>
              <span className="text-subtle">leave empty for default</span>
            </span>
            <Textarea
              rows={4}
              value={form.system_prompt}
              placeholder={agent.default_system_prompt ?? "This agent has no system prompt"}
              onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
              className="font-mono text-[11px]"
            />
          </label>

          <div className="mt-3 flex items-center justify-between">
            <p className="font-mono text-[9.5px] uppercase tracking-wider text-subtle">writes audit log · takes effect next run</p>
            <button type="button" disabled={pending} onClick={save} className={buttonStyles("primary", "sm")}>
              {pending ? <Loader2 className="animate-spin !size-3" /> : <Save className="!size-3" />} save config
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
