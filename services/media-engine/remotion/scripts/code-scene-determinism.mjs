/**
 * Determinism check for code scenes: render the same frame twice and require identical pixels.
 *
 *   node scripts/code-scene-determinism.mjs
 *
 * A GSAP timeline that is seeked per frame must be a pure function of the frame number. This catches
 * wall-clock animation, un-paused timelines and `Math.random()`-alike drift that a single render
 * would happily accept.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {bundle} from "@remotion/bundler";
import {renderStill, selectComposition} from "@remotion/renderer";
import {stageVendor} from "../assets.mjs";
import {SAMPLE_CODE} from "./code-scene-check.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const outDir = path.join(os.tmpdir(), "code-scene-determinism");
fs.mkdirSync(outDir, {recursive: true});

const spec = {
  version: 1,
  title: "Determinism",
  brand: "FACTORY",
  templateId: "youtube-editorial",
  width: 640,
  height: 360,
  fps: 30,
  accent: "#D6FF45",
  background: "#FFFFFF",
  ink: "#111111",
  captions: {enabled: false, mode: "lower-third"},
  defaultTransition: "cut",
  transitionDurationS: 0,
  intro: {enabled: false},
  outro: {enabled: false},
  music: {url: null, volume: 0, duckUnderNarration: false},
  scenes: [
    {
      id: "d1",
      index: 1,
      mode: "code",
      code: SAMPLE_CODE,
      durationS: 4,
      transition: "cut",
      caption: "",
    },
  ],
};

const publicDir = path.join(outDir, "public");
fs.mkdirSync(publicDir, {recursive: true});
stageVendor(publicDir, root);
const serveUrl = await bundle({entryPoint: path.join(root, "src", "index.tsx"), publicDir});
const composition = await selectComposition({serveUrl, id: "youtube-editorial", inputProps: {spec}});

const hashes = [];
for (const run of [1, 2]) {
  const file = path.join(outDir, `frame-90-run${run}.png`);
  await renderStill({composition, serveUrl, inputProps: {spec}, output: file, frame: 90, imageFormat: "png"});
  const digest = crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  hashes.push(digest);
  console.log(`run ${run}: ${digest.slice(0, 16)}…`);
}

if (hashes[0] !== hashes[1]) {
  console.error("FAIL: the same frame rendered differently across runs — animation is not frame-seeked");
  process.exit(1);
}
console.log("PASS: code scenes are deterministic");
process.exit(0);
