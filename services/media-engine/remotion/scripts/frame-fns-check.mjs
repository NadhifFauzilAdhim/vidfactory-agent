/**
 * Does a `__frameFns` hook actually drive the frame?
 *
 * The hook paints a bar whose height encodes the scene time it received. If the runtime never calls
 * the hook, the bar stays at its CSS height.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {execFileSync} from "node:child_process";
import {fileURLToPath} from "node:url";
import {bundle} from "@remotion/bundler";
import {renderMedia, selectComposition} from "@remotion/renderer";
import {stageVendor} from "../assets.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "frame-fns-"));

const JS = `
    var bar = document.querySelector(".bar");
    window.__frameFns.push(function (t, frame) {
      // 1 px per 10 ms of scene time, but the stage is only 270 px tall so cap the probe at 2.4 s
      bar.style.height = Math.max(2, Math.round(Math.min(t, 2.4) * 100)) + "px";
    });`;

const spec = {
  version: 1, title: "hook", brand: "F", templateId: "youtube-editorial",
  width: 480, height: 270, fps: 30, accent: "#D6FF45", background: "#000000", ink: "#FFFFFF",
  captions: {enabled: false, mode: "lower-third"},
  defaultTransition: "cut", transitionDurationS: 0,
  intro: {enabled: false}, outro: {enabled: false},
  music: {url: null, volume: 0, duckUnderNarration: false},
  scenes: [{
    id: "s1", index: 1, mode: "code", durationS: 4, transition: "cut", caption: "",
    code: {
      runtime: 1,
      html: '<div class="bar"></div>',
      css: "body{background:#000}.bar{position:absolute;left:20px;bottom:0;width:40px;height:2px;background:#fff}",
      js: JS,
    },
  }],
};

const publicDir = path.join(outDir, "public");
fs.mkdirSync(publicDir, {recursive: true});
stageVendor(publicDir, root);
const serveUrl = await bundle({entryPoint: path.join(root, "src", "index.tsx"), publicDir});
const composition = await selectComposition({serveUrl, id: "youtube-editorial", inputProps: {spec}});
const video = path.join(outDir, "hook.mp4");
await renderMedia({composition, serveUrl, inputProps: {spec}, codec: "h264",
  outputLocation: video, imageFormat: "jpeg", crf: 12, concurrency: 1, logLevel: "warn"});

function barHeight(file) {
  const raw = execFileSync("ffmpeg", ["-v", "error", "-i", file, "-vf", "crop=40:270:20:0,format=gray",
    "-f", "rawvideo", "-"]);
  for (let y = 0; y < 270; y++) {
    let sum = 0;
    for (let i = 0; i < 40; i++) sum += raw[y * 40 + i];
    if (sum / 40 > 100) return 270 - y;
  }
  return 0;
}

let ok = true;
for (const n of [30, 60, 90]) {
  const file = path.join(outDir, `f${n}.png`);
  execFileSync("ffmpeg", ["-y", "-v", "error", "-i", video, "-vf", `select=eq(n\\,${n})`,
    "-frames:v", "1", file]);
  const height = barHeight(file);
  const expected = Math.round(Math.min(n / 30, 2.4) * 100);
  const pass = Math.abs(height - expected) <= 6;
  ok = ok && pass;
  console.log(`frame ${n}: bar ${height}px, expected ≈${expected}px  ${pass ? "ok" : "MISMATCH"}`);
}

fs.rmSync(outDir, {recursive: true, force: true});
console.log(ok ? "PASS: __frameFns hooks receive the scene time" : "FAIL: hooks not called");
process.exit(ok ? 0 : 1);
