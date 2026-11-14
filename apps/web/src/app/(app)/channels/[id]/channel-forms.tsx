"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Plus, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";

import type { Channel, Character, ChannelSettings, Schedule } from "@/lib/types";
import { Button, buttonStyles } from "@/components/ui/button";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/input";

async function send(path: string, method: string, body?: unknown) {
  const res = await fetch(`/api/proxy${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.error?.message ?? `Request failed (${res.status})`);
  return data;
}

export function ChannelSettingsForm({ channel }: { channel: Channel }) {
  const router = useRouter();
  const s = channel.settings ?? {
    visual_style: "",
    palette: {},
    content_guidelines: "",
    banned_topics: [],
    made_for_kids: true,
    target_duration_seconds: 240,
    cost_ceiling_usd: 4.5,
    approval_mode: "always",
    min_quality_score: 85,
    voice_provider: null,
    voice_id: null,
    thumbnail_style: null,
  };
  const [form, setForm] = useState({
    name: channel.name,
    handle: channel.handle,
    description: channel.description ?? "",
    niche: channel.niche,
    audience: channel.audience,
    language: channel.language,
    video_format: channel.video_format,
    visual_mode: channel.visual_mode ?? "image",
    is_active: channel.is_active,
    settings: { ...s, banned: (s.banned_topics ?? []).join(", ") } as ChannelSettings & { banned: string },
  });
  const [pending, setPending] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function save() {
    setPending(true);
    try {
      const { banned, ...rest } = form.settings;
      await send(`/channels/${channel.id}`, "PATCH", {
        name: form.name,
        handle: form.handle,
        description: form.description || null,
        niche: form.niche,
        audience: form.audience,
        language: form.language,
        video_format: form.video_format,
        visual_mode: form.visual_mode,
        is_active: form.is_active,
        settings: { ...rest, banned_topics: banned.split(",").map((b) => b.trim()).filter(Boolean) },
      });
      toast.success("Channel saved");
      router.refresh();
    } catch (err) {
      toast.error("Could not save", { description: (err as Error).message });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="grid gap-4 md:grid-cols-3">
        <Field label="name"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="handle"><Input value={form.handle} onChange={(e) => setForm({ ...form, handle: e.target.value })} /></Field>
        <Field label="niche"><Input value={form.niche} onChange={(e) => setForm({ ...form, niche: e.target.value })} /></Field>
        <Field label="audience"><Input value={form.audience} onChange={(e) => setForm({ ...form, audience: e.target.value })} /></Field>
        <Field label="language">
          <Input value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })} />
        </Field>
        <Field label="format">
          <Select value={form.video_format} onChange={(e) => setForm({ ...form, video_format: e.target.value as Channel["video_format"] })}>
            <option value="16:9">16:9</option>
            <option value="9:16">9:16</option>
            <option value="1:1">1:1</option>
          </Select>
        </Field>
        <Field
          label="visuals"
          hint={
            form.visual_mode === "generative"
              ? "Remotion composes each scene from its prompt — no image provider used"
              : form.visual_mode === "code"
                ? "The LLM writes each scene as HTML/CSS/GSAP — no image provider used"
                : "One generated image per scene, animated by Remotion"
          }
        >
          <Select
            value={form.visual_mode}
            onChange={(e) =>
              setForm({ ...form, visual_mode: e.target.value as "image" | "generative" | "code" })
            }
          >
            <option value="image">Generated images</option>
            <option value="generative">Remotion only (no images)</option>
            <option value="code">Written by AI (HTML/CSS/GSAP)</option>
          </Select>
        </Field>
      </div>
      <Field label="description">
        <Textarea rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
      </Field>

      <div className="grid gap-4 border-t border-border pt-4 md:grid-cols-3">
        <Field label="visual style" className="md:col-span-2">
          <Input value={form.settings.visual_style} onChange={(e) => setForm({ ...form, settings: { ...form.settings, visual_style: e.target.value } })} />
        </Field>
        <Field label="target duration (s)">
          <Input type="number" min={60} max={1800} value={form.settings.target_duration_seconds} onChange={(e) => setForm({ ...form, settings: { ...form.settings, target_duration_seconds: Number(e.target.value) } })} />
        </Field>
        <Field label="approval policy">
          <Select value={form.settings.approval_mode} onChange={(e) => setForm({ ...form, settings: { ...form.settings, approval_mode: e.target.value as ChannelSettings["approval_mode"] } })}>
            <option value="always">always human</option>
            <option value="below_score">below quality score</option>
            <option value="never">never</option>
          </Select>
        </Field>
        <Field label="min quality score">
          <Input type="number" min={0} max={100} value={form.settings.min_quality_score} onChange={(e) => setForm({ ...form, settings: { ...form.settings, min_quality_score: Number(e.target.value) } })} />
        </Field>
        <Field label="cost ceiling / video (usd)">
          <Input type="number" min={0} step={0.1} value={form.settings.cost_ceiling_usd} onChange={(e) => setForm({ ...form, settings: { ...form.settings, cost_ceiling_usd: Number(e.target.value) } })} />
        </Field>
        <Field label="voice id" hint="provider-specific voice id">
          <Input value={form.settings.voice_id ?? ""} onChange={(e) => setForm({ ...form, settings: { ...form.settings, voice_id: e.target.value } })} />
        </Field>
        <Field label="banned topics" hint="comma separated">
          <Input value={form.settings.banned} onChange={(e) => setForm({ ...form, settings: { ...form.settings, banned: e.target.value } })} />
        </Field>
        <div className="flex items-end gap-4 pb-1">
          <Checkbox
            label="Made for kids"
            checked={form.settings.made_for_kids}
            onChange={(e) => setForm({ ...form, settings: { ...form.settings, made_for_kids: e.target.checked } })}
          />
          <Checkbox label="Active" checked={form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} />
        </div>
      </div>
      <Field label="content guidelines">
        <Textarea rows={3} value={form.settings.content_guidelines ?? ""} onChange={(e) => setForm({ ...form, settings: { ...form.settings, content_guidelines: e.target.value } })} placeholder="Tone, vocabulary level, recurring rules…" />
      </Field>

      <div className="flex items-center justify-between border-t border-border pt-4">
        <button
          type="button"
          disabled={deleting}
          className={buttonStyles("ghost", "sm", "text-danger hover:text-danger")}
          onClick={async () => {
            if (!window.confirm(`Archive ${channel.name}? Existing content is kept.`)) return;
            setDeleting(true);
            try {
              await send(`/channels/${channel.id}`, "DELETE");
              toast.success("Channel archived");
              router.push("/channels");
            } catch (err) {
              toast.error("Could not archive", { description: (err as Error).message });
              setDeleting(false);
            }
          }}
        >
          <Trash2 className="!size-3" /> archive channel
        </button>
        <Button type="button" disabled={pending} onClick={save} variant="primary">
          {pending ? <Loader2 className="animate-spin !size-3" /> : <Save className="!size-3" />} save channel
        </Button>
      </div>
    </div>
  );
}

export function CastEditor({ channel }: { channel: Channel }) {
  const router = useRouter();
  const [rows, setRows] = useState<Character[]>(channel.characters);
  const [pending, setPending] = useState(false);

  async function save() {
    setPending(true);
    try {
      // server assigns ids; strip client-side ids before replacing the cast
      await send(
        `/channels/${channel.id}/characters`,
        "PUT",
        rows.map((row) => ({
          name: row.name,
          role: row.role,
          description: row.description,
          visual_prompt: row.visual_prompt,
          voice_id: row.voice_id ?? null,
          color: row.color,
        })),
      );
      toast.success("Cast saved");
      router.refresh();
    } catch (err) {
      toast.error("Could not save cast", { description: (err as Error).message });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-3">
      {rows.map((c, i) => (
        <div key={i} className="border border-border px-3 py-2">
          <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
            <Field label="name"><Input value={c.name} onChange={(e) => setRows(rows.map((r, n) => (n === i ? { ...r, name: e.target.value } : r)))} /></Field>
            <Field label="role"><Input value={c.role} onChange={(e) => setRows(rows.map((r, n) => (n === i ? { ...r, role: e.target.value } : r)))} /></Field>
            <div className="flex items-end">
              <button type="button" onClick={() => setRows(rows.filter((_, n) => n !== i))} className={buttonStyles("ghost", "xs", "text-danger")} aria-label={`Remove ${c.name}`}>
                <Trash2 className="!size-3" />
              </button>
            </div>
          </div>
          <Field label="locked appearance" className="mt-2">
            <Textarea rows={2} value={c.visual_prompt} onChange={(e) => setRows(rows.map((r, n) => (n === i ? { ...r, visual_prompt: e.target.value } : r)))} placeholder="Defining visual traits repeated in every image prompt" />
          </Field>
        </div>
      ))}
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={() => setRows([...rows, { name: "", role: "companion", description: "", visual_prompt: "", color: "#111111", voice_id: null }])}
          className={buttonStyles("outline", "sm")}
        >
          <Plus className="!size-3" /> add character
        </button>
        <Button type="button" size="sm" disabled={pending} onClick={save}>
          {pending && <Loader2 className="animate-spin !size-3" />} save cast
        </Button>
      </div>
    </div>
  );
}

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

export function ScheduleEditor({ channelId, schedules }: { channelId: string; schedules: Schedule[] }) {
  const router = useRouter();
  const schedule = schedules[0];
  const [form, setForm] = useState({
    name: schedule?.name ?? "Daily production",
    timezone: schedule?.timezone ?? "UTC",
    days_of_week: schedule?.days_of_week ?? DAYS,
    production_count: schedule?.production_count ?? 1,
    start_time: (schedule?.start_time ?? "06:00:00").slice(0, 5),
    publish_time: (schedule?.publish_time ?? "17:00:00").slice(0, 5),
    max_concurrent: schedule?.max_concurrent ?? 1,
    require_approval: schedule?.require_approval ?? true,
    auto_publish: schedule?.auto_publish ?? false,
    is_enabled: schedule?.is_enabled ?? true,
  });
  const [pending, setPending] = useState(false);

  async function save() {
    setPending(true);
    try {
      const body = {
        ...form,
        start_time: `${form.start_time}:00`,
        publish_time: `${form.publish_time}:00`,
      };
      if (schedule) await send(`/schedules/${schedule.id}`, "PATCH", body);
      else await send(`/channels/${channelId}/schedules`, "POST", body);
      toast.success("Schedule saved", { description: "Next run recomputed in the channel timezone." });
      router.refresh();
    } catch (err) {
      toast.error("Schedule rejected", { description: (err as Error).message });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-4">
        <Field label="name" className="sm:col-span-2"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
        <Field label="timezone" hint="IANA name"><Input value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} /></Field>
        <Field label="videos / day"><Input type="number" min={1} max={5} value={form.production_count} onChange={(e) => setForm({ ...form, production_count: Number(e.target.value) })} /></Field>
        <Field label="production start"><Input type="time" value={form.start_time} onChange={(e) => setForm({ ...form, start_time: e.target.value })} /></Field>
        <Field label="publish time"><Input type="time" value={form.publish_time} onChange={(e) => setForm({ ...form, publish_time: e.target.value })} /></Field>
        <Field label="max concurrent"><Input type="number" min={1} max={3} value={form.max_concurrent} onChange={(e) => setForm({ ...form, max_concurrent: Number(e.target.value) })} /></Field>
        <Field label="enabled">
          <Select value={form.is_enabled ? "on" : "off"} onChange={(e) => setForm({ ...form, is_enabled: e.target.value === "on" })}>
            <option value="on">enabled</option>
            <option value="off">paused</option>
          </Select>
        </Field>
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <span className="mono-label mr-2">active days</span>
        {DAYS.map((d) => {
          const on = form.days_of_week.includes(d);
          return (
            <button
              key={d}
              type="button"
              aria-pressed={on}
              onClick={() => setForm({ ...form, days_of_week: on ? form.days_of_week.filter((x) => x !== d) : [...form.days_of_week, d] })}
              className={
                on
                  ? "h-6 border border-ink bg-accent px-2 font-mono text-[10px] uppercase tracking-wider text-ink"
                  : "h-6 border border-border px-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground hover:border-ink"
              }
            >
              {d}
            </button>
          );
        })}
      </div>
      <div className="flex items-center gap-6">
        <Checkbox label="Require approval" checked={form.require_approval} onChange={(e) => setForm({ ...form, require_approval: e.target.checked })} />
        <Checkbox label="Auto-publish after approval" checked={form.auto_publish} onChange={(e) => setForm({ ...form, auto_publish: e.target.checked })} />
      </div>
      <div className="flex justify-end">
        <Button type="button" size="sm" disabled={pending} onClick={save}>
          {pending && <Loader2 className="animate-spin !size-3" />} save schedule
        </Button>
      </div>
    </div>
  );
}

export function YoutubeConnect({ channelId, connected, oauthConfigured }: { channelId: string; connected: boolean; oauthConfigured: boolean }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);

  async function connect() {
    setPending(true);
    try {
      const data = await send(`/channels/${channelId}/youtube/connect`, "POST");
      window.location.href = data.authorization_url;
    } catch (err) {
      toast.error("Cannot start YouTube connection", { description: (err as Error).message });
      setPending(false);
    }
  }

  async function disconnect() {
    if (!window.confirm("Disconnect YouTube? Publishing will be blocked until reconnected.")) return;
    setPending(true);
    try {
      await send(`/channels/${channelId}/youtube/disconnect`, "POST");
      toast.success("YouTube disconnected");
      router.refresh();
    } catch (err) {
      toast.error("Could not disconnect", { description: (err as Error).message });
    } finally {
      setPending(false);
    }
  }

  if (connected) {
    return (
      <Button type="button" variant="outline" size="sm" disabled={pending} onClick={disconnect}>
        disconnect account
      </Button>
    );
  }
  return (
    <Button type="button" variant="accent" size="sm" disabled={pending || !oauthConfigured} onClick={connect} title={oauthConfigured ? undefined : "Google OAuth is not configured on the server"}>
      {pending && <Loader2 className="animate-spin !size-3" />} connect youtube
    </Button>
  );
}
