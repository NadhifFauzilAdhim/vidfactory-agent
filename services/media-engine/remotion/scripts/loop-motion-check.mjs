/**
 * Does a looping GSAP timeline produce different pixels at different times when seeked?
 *
 *   node scripts/loop-motion-check.mjs
 *
 * Renders the same scene at three timestamps and measures the mean pixel difference. A scene whose
 * only motion is an entrance looks frozen once the entrance ends — that is the reported bug.
 * A scene with repeat: -1 keeps changing.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import {execFileSync} from "node:child_process";
import {fileURLToPath} from "node:url";
import {bundle} from "@remotion/bundler";
import {renderMedia, selectComposition} from "@remotion/renderer";
import {stageVendor} from "../assets.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "loop-motion-"));

const SCENE = {
  runtime: 1,
  html: `
    <div class="figure">
      <div class="arm arm-left"></div>
      <div class="arm arm-right"></div>
      <div class="head"></div>
      <div class="body"></div>
    </div>`,
  css: `
    body{background:linear-gradient(180deg,#f6ebdd,#e5c9ad)}
    .figure{position:absolute;left:50%;top:55%;transform:translate(-50%,-50%);width:30vw;height:40vw}
    .head{position:absolute;left:50%;top:0;transform:translateX(-50%);width:30%;height:24%;border-radius:50%;background:#b57c5b}
    .body{position:absolute;left:50%;top:22%;transform:translateX(-50%);width:52%;height:60%;border-radius:28% 28% 20% 20%;background:#7a3e2f}
    .arm{position:absolute;top:26%;width:11%;height:46%;border-radius:8px;background:#b57c5b;transform-origin:50% 4%}
    .arm-left{left:6%}
    .arm-right{right:6%}`,
  js: `
    const tl = gsap.timeline({paused: true});
    tl.from(".figure", {y: 60, opacity: 0, duration: 0.8, ease: "power2.out"}, 0);
    // The part that matters: continuous motion for the whole scene.
    tl.to(".arm-left", {rotation: 26, duration: 0.9, repeat: -1, yoyo: true, ease: "sine.inOut"}, 0.9);
    tl.to(".arm-right", {rotation: -26, duration: 0.9, repeat: -1, yoyo: true, ease: "sine.inOut"}, 0.9);
    tl.to(".head", {y: -8, duration: 2.4, repeat: -1, yoyo: true, ease: "sine.inOut"}, 0.9);
    window.__timelines.push(tl);`,
};

function spec() {
  return {
    version: 1, title: "loop", brand: "F",
    templateId: "youtube-editorial", width: 640, height: 360, fps: 30,
    accent: "#D6FF45", background: "#FFFFFF", ink: "#111111",
    captions: {enabled: false, mode: "lower-third"},
    defaultTransition: "cut", transitionDurationS: 0,
    intro: {enabled: false}, outro: {enabled: false},
    music: {url: null, volume: 0, duckUnderNarration: false},
    scenes: [{id: "s1", index: 1, mode: "code", code: SCENE, durationS: 5, transition: "cut", caption: ""}],
  };
}

function gray(file) {
  return execFileSync(
    "ffmpeg",
    ["-v", "error", "-i", file, "-vf", "scale=96:54,format=gray", "-f", "rawvideo", "-"],
    {maxBuffer: 32 * 1024 * 1024},
  );
}

function diff(a, b) {
  let sum = 0;
  for (let i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
  return sum / a.length;
}

const publicDir = path.join(outDir, "public");
fs.mkdirSync(publicDir, {recursive: true});
stageVendor(publicDir, root);
const serveUrl = await bundle({entryPoint: path.join(root, "src", "index.tsx"), publicDir});
const composition = await selectComposition({serveUrl, id: "youtube-editorial", inputProps: {spec: spec()}});
const video = path.join(outDir, "loop.mp4");
await renderMedia({composition, serveUrl, inputProps: {spec: spec()}, codec: "h264",
  outputLocation: video, imageFormat: "jpeg", crf: 18, concurrency: 2, logLevel: "warn"});

const frame = (n) => {
  const file = path.join(outDir, `f${n}.png`);
  execFileSync("ffmpeg", ["-y", "-v", "error", "-i", video, "-vf", `select=eq(n\\,${n})`,
    "-frames:v", "1", file]);
  return file;
};

// 60, 75, 90 are well after the 0.8 s entrance (24 frames): only looping motion moves here.
const [a, b, c] = [60, 75, 90].map((n) => gray(frame(n)));
const ab = diff(a, b);
const bc = diff(b, c);
console.log(`frame 60→75 diff: ${ab.toFixed(2)}`);
console.log(`frame 75→90 diff: ${bc.toFixed(2)}`);
fs.rmSync(outDir, {recursive: true, force: true});

if (Math.min(ab, bc) < 0.5) {
  console.error("FAIL: the scene is frozen after its entrance — looping motion is not applied");
  process.exit(1);
}
console.log("PASS: the scene keeps moving");
