"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Plus } from "lucide-react";
import { toast } from "sonner";

import { buttonStyles } from "@/components/ui/button";
import { Field, Input, Select, Textarea } from "@/components/ui/input";

export function NewChannelForm() {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [form, setForm] = useState({
    name: "",
    handle: "",
    niche: "kids-stories",
    audience: "Children 3-7",
    language: "en",
    video_format: "16:9",
    visual_mode: "image",
    description: "",
    visual_style: "Soft gouache storybook illustration",
    made_for_kids: "true",
    approval_mode: "always",
  });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setPending(true);
    try {
      const res = await fetch("/api/proxy/channels", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          name: form.name,
          handle: form.handle || form.name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""),
          niche: form.niche,
          audience: form.audience,
          language: form.language,
          video_format: form.video_format,
          visual_mode: form.visual_mode,
          description: form.description || null,
          settings: {
            visual_style: form.visual_style,
            palette: { primary: "#D6FF45" },
            banned_topics: [],
            made_for_kids: form.made_for_kids === "true",
            target_duration_seconds: 240,
            cost_ceiling_usd: 4.5,
            approval_mode: form.approval_mode,
            min_quality_score: 85,
            content_guidelines: null,
            voice_provider: null,
            voice_id: null,
            thumbnail_style: null,
          },
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error?.message ?? "Could not create channel");
      toast.success("Channel created", { description: "Configure cast, schedule and YouTube next." });
      router.push(`/channels/${data.id}`);
    } catch (err) {
      toast.error("Channel rejected", { description: (err as Error).message });
      setPending(false);
    }
  }

  return (
    <form onSubmit={submit} className="grid gap-4 md:grid-cols-3">
      <Field label="name" htmlFor="new-name" className="md:col-span-1">
        <Input id="new-name" required minLength={2} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Little Lantern Stories" />
      </Field>
      <Field label="handle" htmlFor="new-handle" hint="lowercase, no spaces">
        <Input id="new-handle" value={form.handle} onChange={(e) => setForm({ ...form, handle: e.target.value })} placeholder="littlelantern" />
      </Field>
      <Field label="niche" htmlFor="new-niche">
        <Input id="new-niche" value={form.niche} onChange={(e) => setForm({ ...form, niche: e.target.value })} />
      </Field>
      <Field label="audience">
        <Input value={form.audience} onChange={(e) => setForm({ ...form, audience: e.target.value })} />
      </Field>
      <Field label="language">
        <Select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}>
          {["en", "es", "pt", "fr", "de", "hi", "id", "nl", "ar", "ja"].map((l) => (
            <option key={l} value={l}>{l}</option>
          ))}
        </Select>
      </Field>
      <Field label="format">
        <Select value={form.video_format} onChange={(e) => setForm({ ...form, video_format: e.target.value })}>
          <option value="16:9">16:9 landscape</option>
          <option value="9:16">9:16 vertical</option>
          <option value="1:1">1:1 square</option>
        </Select>
      </Field>
      <Field
        label="visuals"
        hint={
          form.visual_mode === "generative"
            ? "no image provider needed"
            : form.visual_mode === "code"
              ? "the LLM writes each scene as HTML/CSS/GSAP — no image provider needed"
              : "one image per scene"
        }
      >
        <Select value={form.visual_mode} onChange={(e) => setForm({ ...form, visual_mode: e.target.value })}>
          <option value="image">Generated images</option>
          <option value="generative">Remotion only (no images)</option>
          <option value="code">Written by AI (HTML/CSS/GSAP)</option>
        </Select>
      </Field>
      <Field label="visual style" className="md:col-span-2">
        <Input value={form.visual_style} onChange={(e) => setForm({ ...form, visual_style: e.target.value })} />
      </Field>
      <Field label="made for kids">
        <Select value={form.made_for_kids} onChange={(e) => setForm({ ...form, made_for_kids: e.target.value })}>
          <option value="true">yes — made for kids</option>
          <option value="false">no — general audience</option>
        </Select>
      </Field>
      <Field label="description" className="md:col-span-2">
        <Textarea rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="What the channel is about, in one or two sentences." />
      </Field>
      <Field label="approval policy">
        <Select value={form.approval_mode} onChange={(e) => setForm({ ...form, approval_mode: e.target.value })}>
          <option value="always">always human · recommended</option>
          <option value="below_score">below quality score</option>
          <option value="never">never · not recommended</option>
        </Select>
      </Field>
      <div className="flex items-end justify-end md:col-span-3">
        <button type="submit" disabled={pending || form.name.length < 2} className={buttonStyles("primary", "md")}>
          {pending ? <Loader2 className="animate-spin !size-3" /> : <Plus className="!size-3" />} create channel
        </button>
      </div>
    </form>
  );
}
