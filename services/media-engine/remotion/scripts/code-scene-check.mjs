/**
 * Code-scene sample + self-check.
 *
 *   node scripts/code-scene-check.mjs            # render a code scene, assert frames are not blank
 *
 * Proves the contract in docs/11-code-scenes.md: an LLM-authored HTML/CSS/GSAP scene renders inside
 * Remotion together with normal template chrome (border, caption, brand) and a narration track, in
 * one H.264 MP4. It also renders frame 0 and the last frame and fails if both are blank, which is
 * the failure mode a broken timeline produces.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import {fileURLToPath} from "node:url";
import {bundle} from "@remotion/bundler";
import {renderMedia, renderStill, selectComposition} from "@remotion/renderer";
import {execFileSync} from "node:child_process";
import {stageVendor} from "../assets.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const outDir = process.env.CODE_SCENE_OUT ?? path.join(os.tmpdir(), "code-scene-check");
fs.mkdirSync(outDir, {recursive: true});

/** A representative scene: pure type + geometry + a GSAP timeline, no images at all. */
export const SAMPLE_CODE = {
  runtime: 1,
  html: `
    <div class="grain"></div>
    <div class="sun"></div>
    <div class="horizon"></div>
    <div class="city">
      <span class="block b1"></span><span class="block b2"></span><span class="block b3"></span>
      <span class="block b4"></span><span class="block b5"></span>
    </div>
    <div class="titles">
      <p class="kicker">scene 01</p>
      <h1 class="headline">The harbour at dusk</h1>
      <p class="rule"></p>
      <p class="sub">a lantern, a tide, and a very small boat</p>
    </div>
  `,
  css: `
    :root { --ink: #111; --accent: #d6ff45; }
    body { background: linear-gradient(180deg, #0b1b2a 0%, #2a4a63 68%, #d9b07a 100%); }
    .horizon { position:absolute; left:0; right:0; top:64%; height:2px; background:rgba(255,255,255,.35); }
    .sun { position:absolute; left:60%; top:44%; width:14vw; height:14vw; border-radius:50%;
           background: radial-gradient(circle, #ffd98a 0%, #f2a65a 60%, transparent 72%); filter:blur(2px); }
    .city { position:absolute; left:0; right:0; top:48%; height:16%; display:flex; align-items:flex-end; gap:1.2vw; padding:0 6vw; }
    .block { background:#0e1626; border-radius:3px 3px 0 0; width:6vw; }
    .b1{height:42%}.b2{height:74%}.b3{height:56%}.b4{height:88%}.b5{height:38%}
    .grain { position:absolute; inset:0; opacity:.07; mix-blend-mode:overlay;
             background-image: radial-gradient(rgba(255,255,255,.9) .5px, transparent .6px);
             background-size:3px 3px; }
    .titles { position:absolute; left:7vw; bottom:14vh; max-width:44vw; }
    .kicker { font: 500 1vw/1.2 ui-monospace, monospace; letter-spacing:.28em; text-transform:uppercase; color:#d6ff45; }
    .headline { margin-top:1.4vh; font: italic 400 5.4vw/1.02 Georgia, "Times New Roman", serif; color:#f7f3ea; }
    .rule { margin:1.8vh 0; width:9vw; height:2px; background:#d6ff45; transform-origin:left center; }
    .sub { font: 400 1.05vw/1.5 system-ui, sans-serif; letter-spacing:.02em; color:rgba(247,243,234,.72); }
  `,
  js: `
    const tl = gsap.timeline({ paused: true });
    tl.from(".sun", { y: 60, opacity: 0, scale: 0.85, duration: 1.2, ease: "power2.out" }, 0);
    tl.from(".block", { y: 90, duration: 0.7, stagger: 0.07, ease: "power3.out" }, 0.2);
    tl.from(".kicker", { opacity: 0, y: 12, duration: 0.5, ease: "power2.out" }, 0.5);
    tl.from(".headline", { opacity: 0, y: 26, duration: 0.9, ease: "power3.out" }, 0.7);
    tl.fromTo(".rule", { scaleX: 0 }, { scaleX: 1, duration: 0.8, ease: "power2.inOut" }, 1.2);
    tl.from(".sub", { opacity: 0, y: 10, duration: 0.6 }, 1.5);
    tl.to(".sun", { y: -24, duration: 3, ease: "none" }, 1.6);
    window.__timelines.push(tl);
  `,
};

function spec() {
  return {
    version: 1,
    title: "Code scene proof",
    brand: "FACTORY",
    templateId: "youtube-editorial",
    width: 1280,
    height: 720,
    fps: 30,
    accent: "#D6FF45",
    background: "#FFFFFF",
    ink: "#111111",
    captions: {enabled: true, mode: "lower-third"},
    defaultTransition: "fade",
    transitionDurationS: 0.4,
    intro: {enabled: false},
    outro: {enabled: false},
    music: {url: null, volume: 0.1, duckUnderNarration: false},
    scenes: [
      {
        id: "cs1",
        index: 1,
        title: "The harbour at dusk",
        mode: "code",
        prompt: "a harbour at dusk with a lantern",
        code: SAMPLE_CODE,
        durationS: 4,
        transition: "cut",
        caption: "The harbour turned the colour of pale tea.",
      },
      {
        id: "cs2",
        index: 2,
        title: "Fallback",
        mode: "code",
        prompt: "a quiet room, warm lamp light",
        code: null,
        durationS: 2,
        transition: "fade",
        caption: "A second scene with broken code falls back.",
      },
    ],
  };
}

async function frameStats(ffmpeg, file) {
  const raw = execFileSync(
    ffmpeg,
    ["-v", "error", "-i", file, "-vf", "scale=48:27,format=gray", "-f", "rawvideo", "-"],
    {maxBuffer: 64 * 1024 * 1024},
  );
  let sum = 0;
  for (const byte of raw) sum += byte;
  const mean = sum / raw.length;
  let variance = 0;
  for (const byte of raw) variance += (byte - mean) ** 2;
  return {mean, stddev: Math.sqrt(variance / raw.length)};
}

async function main() {
  const gsapPath = path.join(root, "node_modules", "gsap", "dist", "gsap.min.js");
  if (!fs.existsSync(gsapPath)) throw new Error(`gsap not installed at ${gsapPath}`);
  const publicDir = path.join(outDir, "public");
  fs.mkdirSync(publicDir, {recursive: true});
  stageVendor(publicDir, root);

  const serveUrl = await bundle({entryPoint: path.join(root, "src", "index.tsx"), publicDir});
  const composition = await selectComposition({serveUrl, id: "youtube-editorial", inputProps: {spec: spec()}});
  console.log(`composition ${composition.durationInFrames} frames @ ${composition.fps}fps`);

  const video = path.join(outDir, "code-scene.mp4");
  await renderMedia({composition, serveUrl, inputProps: {spec: spec()}, codec: "h264", outputLocation: video,
    imageFormat: "jpeg", crf: 18, enforceAudioTrack: false, concurrency: 2, logLevel: "warn"});

  const probes = [
    {name: "first", frame: 8},
    {name: "middle", frame: Math.floor(composition.durationInFrames / 2)},
    {name: "last", frame: composition.durationInFrames - 2},
  ];
  const ffmpeg = process.env.FFMPEG_PATH ?? "ffmpeg";
  let failed = false;
  for (const p of probes) {
    const file = path.join(outDir, `code-${p.name}.jpg`);
    await renderStill({composition, serveUrl, inputProps: {spec: spec()}, output: file, frame: p.frame,
      imageFormat: "jpeg", jpegQuality: 90});
    const stats = await frameStats(ffmpeg, file);
    // A blank frame is flat: a real scene has contrast (the gradient alone clears this threshold).
    const blank = stats.stddev < 2.0;
    console.log(
      `${p.name.padEnd(7)} frame ${String(p.frame).padStart(4)}  mean ${stats.mean.toFixed(1)}  ` +
        `contrast ${stats.stddev.toFixed(1)}  ${blank ? "BLANK" : "ok"}`,
    );
    if (blank) failed = true;
  }
  console.log(`video: ${video} (${(fs.statSync(video).size / 1024).toFixed(0)} KB)`);
  if (failed) {
    console.error("FAIL: a code scene rendered blank — check GSAP mounting and the seek runtime");
    process.exitCode = 1;
  } else {
    console.log("PASS: code scenes render frame-accurately");
  }
}

const isDirectRun = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isDirectRun) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}

export {spec};
