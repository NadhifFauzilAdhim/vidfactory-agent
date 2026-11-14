/**
 * Server-side render CLI — the only entry point the Factory API uses.
 *
 *   node render.mjs --job <job.json>
 *
 * Assets are staged in a temporary public directory and referenced with `staticFile()`-style
 * relative paths (browsers block file:// URLs), so the same composition previews in the Studio
 * from `public/` and renders from the staged copy. Progress is streamed to stdout as JSON lines
 * for the worker to persist; cancellation uses Remotion's own cancel signal.
 *
 * Everything here calls @remotion/renderer (bundle → selectComposition → renderMedia/renderStill).
 * There is no custom encoding or FFmpeg orchestration in this project.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import {bundle} from "@remotion/bundler";
import {makeCancelSignal, renderMedia, renderStill, selectComposition} from "@remotion/renderer";
import {stageVendor} from "./assets.mjs";

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  args.set(process.argv[i].replace(/^--/, ""), process.argv[i + 1]);
}

function emit(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

const jobPath = args.get("job");
if (!jobPath) {
  emit({type: "error", message: "missing --job <path>"});
  process.exit(2);
}

/** Copy an asset into <publicDir>/jobs/<renderId>/<name>; returns its bundle-relative path. */
function stageAsset(publicDir, renderId, source, name) {
  const destDir = path.join(publicDir, "jobs", renderId);
  fs.mkdirSync(destDir, {recursive: true});
  const dest = path.join(destDir, name);
  if (!fs.existsSync(dest)) fs.copyFileSync(source, dest);
  return `jobs/${renderId}/${name}`;
}

function publicUrl(asset, renderId) {
  return `jobs/${renderId}/${asset.name}`;
}

/**
 * Unique, extension-preserving file name. The extension matters: Remotion's image loader and its
 * audio extraction both sniff by extension, so "img-1" without ".png" fails to load.
 */
function assetName(asset) {
  if (asset.name) return asset.name;
  const base = path.basename(asset.source ?? asset.key ?? "asset");
  return `${String(asset.key ?? "a").replace(/[^a-z0-9-]/gi, "_")}-${base}`;
}

async function main() {
  const job = JSON.parse(fs.readFileSync(jobPath, "utf8"));
  const renderId = job.renderId ?? `r-${Date.now()}`;
  const publicDir = job.publicDir ? path.resolve(job.publicDir) : path.join(os.tmpdir(), `factory-public-${renderId}`);
  fs.mkdirSync(publicDir, {recursive: true});

  // Stage assets: each entry is {kind: "image"|"audio", source: absolutePath, name: uniqueName}
  // Stage assets: each entry is {key, source: absolutePath, name?: uniqueFileName}
  const assets = (job.assets ?? []).map((asset) => ({...asset, name: assetName(asset)}));
  for (const asset of assets) {
    if (!fs.existsSync(asset.source)) {
      throw new Error(`asset missing: ${asset.source}`);
    }
    stageAsset(publicDir, renderId, asset.source, asset.name);
  }

  // Code scenes read their scene programme from a staged file and their animation library from the
  // shared vendor dir. Both must exist before the bundle is built.
  stageVendor(publicDir, import.meta.dirname);

  const inputProps = JSON.parse(
    JSON.stringify(job.inputProps)
      .replace(/"assetKey":"([^"]+)"/g, (_m, key) => {
        const asset = assets.find((a) => a.key === key);
        if (!asset) throw new Error(`unknown assetKey ${key}`);
        return `"imageSrc":"${publicUrl(asset, renderId)}"`;
      })
      .replace(/"narrationKey":"([^"]+)"/g, (_m, key) => {
        const asset = assets.find((a) => a.key === key);
        if (!asset) throw new Error(`unknown narrationKey ${key}`);
        return `"narrationSrc":"${publicUrl(asset, renderId)}"`;
      })
      .replace(/"codeKey":"([^"]+)"/g, (_m, key) => {
        const asset = assets.find((a) => a.key === key);
        if (!asset) throw new Error(`unknown codeKey ${key}`);
        return `"codeSrc":"${publicUrl(asset, renderId)}"`;
      }),
  );

  // Inline scene programmes into the props: the composition must not depend on an async fetch (a
  // missing file would silently render the generative fallback). Code scenes are small (tens of KB
  // for a whole video), and load-bearing content belongs in the props it is rendered from.
  for (const scene of inputProps?.spec?.scenes ?? []) {
    if (!scene || !scene.codeSrc) continue;
    const file = path.join(publicDir, scene.codeSrc);
    scene.code = JSON.parse(fs.readFileSync(file, "utf8"));
    delete scene.codeSrc;
  }

  const cancelFile = job.cancelFile ? path.resolve(job.cancelFile) : null;
  const {cancelSignal, cancel} = makeCancelSignal();
  let timer = null;
  if (cancelFile) {
    timer = setInterval(() => {
      if (fs.existsSync(cancelFile)) cancel();
    }, 500);
  }
  const stop = () => {
    if (timer) clearInterval(timer);
  };

  try {
    const startedAt = Date.now();
    const serveUrl = await bundle({
      entryPoint: path.resolve(job.entryPoint ?? "src/index.tsx"),
      publicDir,
      onProgress: (percent) => emit({type: "bundle", progress: percent}),
    });

    const composition = await selectComposition({
      serveUrl,
      id: job.compositionId,
      inputProps,
      chromiumOptions: {gl: job.gl ?? "angle"},
    });
    emit({
      type: "composition",
      durationInFrames: composition.durationInFrames,
      fps: composition.fps,
      width: composition.width,
      height: composition.height,
    });

    if (Array.isArray(job.stills) && job.stills.length > 0) {
      for (const still of job.stills) {
        await renderStill({
          composition,
          serveUrl,
          inputProps,
          output: path.resolve(still.path),
          frame: still.frame,
          imageFormat: "jpeg",
          jpegQuality: 85,
          chromiumOptions: {gl: job.gl ?? "angle"},
        });
        emit({type: "still", path: still.path, frame: still.frame});
      }
    }

    if (job.stillsOnly) {
      emit({type: "done", path: null, bytes: 0, elapsedMs: Date.now() - startedAt});
      return;
    }

    await renderMedia({
      composition,
      serveUrl,
      inputProps,
      codec: job.codec ?? "h264",
      outputLocation: path.resolve(job.outputPath),
      imageFormat: "jpeg",
      jpegQuality: job.jpegQuality ?? 90,
      crf: job.crf ?? 20,
      x264Preset: job.x264Preset ?? "veryfast",
      audioCodec: "aac",
      audioBitrate: job.audioBitrate ?? "192k",
      enforceAudioTrack: true,
      concurrency: job.concurrency ?? null,
      cancelSignal,
      overwrite: true,
      logLevel: "warn",
      // Code scenes mount a nested document per scene; give each frame more headroom than the
      // default 60 s without letting a hung scene stall the whole job.
      timeoutInMilliseconds: job.frameTimeoutMs ?? 120000,
      chromiumOptions: {gl: job.gl ?? "angle"},
      onStart: ({frameCount, resolvedConcurrency}) =>
        emit({type: "start", frameCount, concurrency: resolvedConcurrency}),
      onProgress: ({progress, renderedFrames, encodedFrames, stitchStage}) =>
        emit({
          type: "progress",
          progress,
          renderedFrames,
          encodedFrames,
          stage: stitchStage,
          elapsedMs: Date.now() - startedAt,
        }),
      onBrowserLog: (log) => {
        if (log.type === "error") emit({type: "browser-error", message: log.text});
      },
    });

    const stat = fs.statSync(path.resolve(job.outputPath));
    emit({
      type: "done",
      path: job.outputPath,
      bytes: stat.size,
      elapsedMs: Date.now() - startedAt,
    });
  } catch (error) {
    if (fs.existsSync(cancelFile ?? "")) {
      emit({type: "cancelled"});
    } else {
      emit({type: "error", message: error?.message ?? String(error), stack: error?.stack ?? null});
      process.exitCode = 1;
    }
  } finally {
    stop();
  }
}

main().catch((error) => {
  emit({type: "error", message: error?.message ?? String(error)});
  process.exit(1);
});
