import Link from "next/link";
import { notFound } from "next/navigation";
import { AlertTriangle, ArrowLeft, ArrowUpRight, Check, Download, Film, Minus, X } from "lucide-react";

import { ApiError, assetUrl, resources } from "@/lib/api";
import { formatBytes, formatDuration, formatUsd, formatWhen } from "@/lib/format";
import type { ProjectDetail } from "@/lib/types";
import { buttonStyles } from "@/components/ui/button";
import { PageHeader, Section, SectionTitle } from "@/components/ui/layout";
import { MetricStrip } from "@/components/ui/metrics";
import { StatusChip, TagBox } from "@/components/ui/status";
import { DataTable, TD, TH, THead, TR } from "@/components/ui/table";

import { ApprovalDecision, MetadataEditor, SceneRegenerate } from "./actions";
import { SceneCodeViewer } from "./scene-code";

export const metadata = { title: "Project" };

type Seo = {
  title?: string;
  description?: string;
  tags?: string[];
  thumbnail_text?: string;
  thumbnail_concept?: string;
  playlist?: string;
};

export default async function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let project: ProjectDetail;
  try {
    project = await resources.project(id);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) notFound();
    throw err;
  }

  const seo = (project.metadata.seo ?? {}) as Seo;
  const brief = project.metadata.brief as { brief?: string; creative_direction?: string } | undefined;
  const render = project.renders[0];
  const pending = project.status === "awaiting_approval" && project.approval_id;

  return (
    <>
      <PageHeader
        breadcrumbs={[
          { label: "Factory" },
          { label: "Content", href: "/content" },
          { label: project.title.length > 42 ? `${project.title.slice(0, 42)}…` : project.title },
        ]}
        title={project.title}
        description={brief?.brief ?? project.script?.description ?? "No brief recorded for this project."}
        meta={
          <>
            <StatusChip tone={project.status === "published" || project.status === "approved" ? "live" : project.status === "failed" ? "danger" : project.status === "awaiting_approval" ? "warning" : "ink"}>
              {project.status.replaceAll("_", " ")}
            </StatusChip>
            <span>{project.channel_name}</span>
          </>
        }
        actions={
          <>
            <Link href="/content" className={buttonStyles("ghost", "md")}>
              <ArrowLeft className="!size-3" /> library
            </Link>
            {project.workflow_id && (
              <Link href={`/workflows/${project.workflow_id}`} className={buttonStyles("secondary", "md")}>
                workflow <ArrowUpRight className="!size-3" />
              </Link>
            )}
            {render?.url && (
              <>
                <a href={assetUrl(render.url)} target="_blank" rel="noreferrer" className={buttonStyles("secondary", "md")}>
                  <Film className="!size-3" /> preview
                </a>
                <a href={`/api/proxy/content/${project.id}/download`} className={buttonStyles("primary", "md")}>
                  <Download className="!size-3" /> download mp4
                </a>
              </>
            )}
          </>
        }
      />

      <div className="mt-6">
        <MetricStrip
          metrics={[
            { label: "qc score", value: project.qc ? String(project.qc.score) : "—", caption: project.qc?.status ?? "not reviewed", accent: !!project.qc && project.qc.status === "pass" },
            { label: "scenes", value: String(project.scenes.length), caption: "storyboard shots" },
            { label: "duration", value: formatDuration(render?.duration_s || project.estimated_duration_s), caption: render ? "rendered" : "estimated" },
            { label: "render", value: render ? formatBytes(render.bytes) : "—", caption: render?.resolution ?? "not rendered" },
            { label: "words", value: String(project.script?.word_count ?? "—"), caption: "script length" },
            { label: "cost", value: formatUsd(project.total_cost_usd, 3), caption: "all stages" },
            { label: "language", value: project.language.toUpperCase(), caption: project.format },
            { label: "created", value: formatWhen(project.created_at), caption: project.channel_name },
          ]}
        />
      </div>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0">
          {/* render */}
          <Section className="!mt-0">
            <SectionTitle title="Render" index="01" caption={render ? `${render.resolution} · ${formatDuration(render.duration_s)}` : "pending"} />
            {render?.url ? (
              <div className="border border-ink bg-ink">
                <video
                  controls
                  preload="metadata"
                  className="aspect-video w-full bg-ink"
                  src={assetUrl(render.url)}
                >
                  Your browser does not support video playback.
                </video>
                <div className="flex items-center justify-between border-t border-white/10 px-3 py-1.5 font-mono text-[9.5px] uppercase tracking-wider text-white/50">
                  <span>render {render.id.slice(0, 8)} · {render.status}</span>
                  <span>{formatBytes(render.bytes)}</span>
                </div>
              </div>
            ) : project.renders.length > 0 ? (
              <div className="border border-warning bg-[#FDF4DE] px-3 py-2 font-mono text-[11px] uppercase tracking-wider text-[#8A6412]">
                render {project.renders[0].status} — no playable file yet
              </div>
            ) : (
              <div className="border border-dashed border-border bg-paper px-3 py-6 text-[13px] text-muted-foreground">
                This project has not been rendered yet. Run the pipeline (Workflows → retry) to produce a video.
              </div>
            )}
          </Section>

          {/* scenes */}
          <Section>
            <SectionTitle
              title="Storyboard"
              index="02"
              caption={`${project.scenes.length} scenes`}
              actions={<span className="mono-label">image · narration · prompt</span>}
            />
            <ol className="divide-y divide-border">
              {project.scenes.map((s) => (
                <li key={s.id} className="grid gap-3 py-4 sm:grid-cols-[180px_minmax(0,1fr)]">
                  <div className="relative aspect-video overflow-hidden border border-border bg-muted">
                    {s.image_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={assetUrl(s.image_url)} alt={`Scene ${s.index}`} className="size-full object-cover" loading="lazy" />
                    ) : (
                      <div className="grid size-full place-items-center">
                        <Minus className="size-4 text-subtle" />
                      </div>
                    )}
                    <span className="absolute left-1 top-1 bg-ink px-1 font-mono text-[9px] text-white">
                      {String(s.index).padStart(2, "0")}
                    </span>
                  </div>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-[13px] font-medium">{s.title}</p>
                      <span className="flex items-center gap-1.5">
                        <TagBox>{s.camera ?? "shot"}</TagBox>
                        <TagBox>{s.transition}</TagBox>
                        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                          {formatDuration(s.audio_duration_s ?? s.duration_s)}
                        </span>
                      </span>
                    </div>
                    <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">{s.narration}</p>
                    {s.dialogue.length > 0 && (
                      <ul className="mt-1.5 space-y-0.5">
                        {s.dialogue.map((d, i) => (
                          <li key={i} className="text-[12px]">
                            <span className="font-mono text-[10px] uppercase tracking-wider text-ink">{d.character}</span>
                            <span className="text-muted-foreground"> · “{d.line}”</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    <details className="mt-2">
                      <summary className="cursor-pointer font-mono text-[9.5px] uppercase tracking-wider text-muted-foreground hover:text-ink">
                        image prompt
                      </summary>
                      <p className="mt-1 border-l border-border pl-2 font-mono text-[10.5px] leading-relaxed text-muted-foreground">
                        {s.image_prompt}
                      </p>
                    </details>
                    <div className="mt-2 flex items-center gap-2">
                      <SceneRegenerate projectId={project.id} sceneIndex={s.index} target="image" hasAsset={!!s.image_url} />
                      <SceneRegenerate projectId={project.id} sceneIndex={s.index} target="audio" hasAsset={!!s.audio_url} />
                      {s.audio_url && (
                        <audio controls preload="none" src={assetUrl(s.audio_url)} className="h-6 max-w-[220px]" />
                      )}
                    </div>
                    {s.code_url && <SceneCodeViewer codeUrl={s.code_url} />}
                  </div>
                </li>
              ))}
              {project.scenes.length === 0 && (
                <li className="py-4 text-[13px] text-muted-foreground">
                  No storyboard yet — the script and storyboard agents have not completed for this project.
                </li>
              )}
            </ol>
          </Section>

          {/* script */}
          {project.script && (
            <Section>
              <SectionTitle title="Script" index="03" caption={`v${project.script.version} · ${project.script.word_count} words`} />
              {project.script.hook && (
                <p className="mb-3 border-l-2 border-accent pl-3 font-serif text-[18px] leading-snug">{project.script.hook}</p>
              )}
              <div className="max-h-[360px] overflow-y-auto whitespace-pre-wrap border border-border bg-paper px-4 py-3 text-[13px] leading-[1.75] scroll-thin">
                {project.script.narration}
              </div>
            </Section>
          )}
        </div>

        {/* side column */}
        <div className="space-y-6">
          {pending && (
            <div>
              <p className="mono-label mb-2 text-ink">status · awaiting approval</p>
              <ApprovalDecision approvalId={String(pending)} projectId={project.id} />
            </div>
          )}

          {project.qc && (
            <div className="border border-border">
              <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
                <span className="mono-label text-ink">quality control</span>
                <StatusChip tone={project.qc.status === "pass" ? "live" : project.qc.status === "fail" ? "danger" : "warning"}>
                  {project.qc.status}
                </StatusChip>
              </div>
              <div className="flex items-baseline gap-2 px-3 pt-3">
                <span className="mono-value text-[34px] leading-none">{project.qc.score}</span>
                <span className="mono-label">/ 100</span>
              </div>
              <ul className="mt-3 divide-y divide-border border-t border-border">
                {project.qc.checks.map((c) => (
                  <li key={c.id} className="flex items-start gap-2 px-3 py-1.5">
                    {c.passed ? (
                      <Check className="mt-0.5 size-3 shrink-0 text-[#4B6B0D]" />
                    ) : (
                      <X className="mt-0.5 size-3 shrink-0 text-danger" />
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="block text-[12px] leading-tight">{c.label}</span>
                      <span className="block font-mono text-[9.5px] uppercase tracking-wider text-muted-foreground">{c.note}</span>
                    </span>
                    <span className="font-mono text-[9.5px] text-subtle">{Math.round(c.weight * 100)}%</span>
                  </li>
                ))}
              </ul>
              {(project.qc.issues.length > 0 || project.qc.warnings.length > 0) && (
                <div className="border-t border-border px-3 py-2">
                  {project.qc.issues.map((i, n) => (
                    <p key={n} className="flex gap-1.5 py-0.5 text-[11.5px] text-[#9B2C28]">
                      <AlertTriangle className="mt-0.5 size-3 shrink-0" /> {i.message}
                    </p>
                  ))}
                  {project.qc.warnings.map((w, n) => (
                    <p key={`w${n}`} className="flex gap-1.5 py-0.5 text-[11.5px] text-[#8A6412]">
                      <AlertTriangle className="mt-0.5 size-3 shrink-0" /> {w}
                    </p>
                  ))}
                </div>
              )}
              {project.qc.recommendations.length > 0 && (
                <ul className="border-t border-border px-3 py-2">
                  {project.qc.recommendations.map((r, n) => (
                    <li key={n} className="flex gap-1.5 py-0.5 text-[11.5px] text-muted-foreground">
                      <span className="text-subtle">→</span> {r}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="border border-border">
            <div className="flex items-center justify-between border-b border-border px-3 py-1.5">
              <span className="mono-label text-ink">seo & metadata</span>
              <MetadataEditor projectId={project.id} seo={seo} />
            </div>
            <div className="px-3 py-3">
              <p className="mono-label">title</p>
              <p className="mt-1 text-[13px] leading-snug">{seo.title ?? "not generated"}</p>
              <p className="mono-label mt-3">description</p>
              <p className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-muted-foreground">
                {seo.description ?? "—"}
              </p>
              <p className="mono-label mt-3">tags</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {(seo.tags ?? []).map((t) => (
                  <TagBox key={t}>{t}</TagBox>
                ))}
                {!seo.tags?.length && <span className="text-[12px] text-muted-foreground">—</span>}
              </div>
              {seo.thumbnail_text && (
                <>
                  <p className="mono-label mt-3">thumbnail text</p>
                  <p className="mt-1 font-serif text-[18px]">“{seo.thumbnail_text}”</p>
                  <p className="mt-1 text-[11.5px] text-muted-foreground">{seo.thumbnail_concept}</p>
                </>
              )}
              {project.thumbnail_url && (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={assetUrl(project.thumbnail_url)} alt="Thumbnail" className="mt-2 aspect-video w-full border border-border object-cover" />
              )}
              {seo.playlist && (
                <p className="mt-2 font-mono text-[9.5px] uppercase tracking-wider text-muted-foreground">playlist · {seo.playlist}</p>
              )}
            </div>
          </div>

          <div className="border border-border">
            <div className="border-b border-border px-3 py-1.5">
              <span className="mono-label text-ink">assets</span>
            </div>
            <DataTable>
              <THead>
                <TH>kind</TH>
                <TH>provider</TH>
                <TH align="right">size</TH>
              </THead>
              <tbody>
                {project.assets.slice(-10).map((a) => (
                  <TR key={a.id}>
                    <TD>
                      <a href={assetUrl(a.url)} target="_blank" rel="noreferrer" className="font-mono text-[10.5px] uppercase tracking-wider hover:underline">
                        {a.kind}
                      </a>
                    </TD>
                    <TD mono className="text-muted-foreground">{a.provider ?? "—"}</TD>
                    <TD align="right" mono className="text-muted-foreground">{formatBytes(a.bytes)}</TD>
                  </TR>
                ))}
                {project.assets.length === 0 && (
                  <TR><TD align="center" className="py-3 text-muted-foreground">No assets</TD></TR>
                )}
              </tbody>
            </DataTable>
          </div>
        </div>
      </div>
    </>
  );
}
