/**
 * Build-time engine verification (fast, no video render).
 *
 *   node scripts/verify-engine.mjs
 *
 * Catches the two failures that would otherwise only surface on the first real render inside a
 * container: a composition that does not bundle, and a code-scene dependency (GSAP) that was never
 * installed. Rendering itself is exercised at runtime by
 * `infrastructure/docker/container_check.py`, which is slower but closer to production.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import {fileURLToPath} from "node:url";
import {bundle} from "@remotion/bundler";
import {stageVendor, VENDOR_FILES} from "../assets.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// 1 · Every vendored engine asset must exist in node_modules.
for (const file of VENDOR_FILES) {
  const source = path.join(root, "node_modules", file.from);
  if (!fs.existsSync(source)) {
    throw new Error(`missing engine asset: ${file.from} (check package.json / npm ci)`);
  }
}

// 2 · Staging must produce the file the composition fetches at render time.
const publicDir = fs.mkdtempSync(path.join(os.tmpdir(), "engine-verify-"));
stageVendor(publicDir, root);
for (const file of VENDOR_FILES) {
  const staged = path.join(publicDir, file.to);
  if (!fs.existsSync(staged) || fs.statSync(staged).size < 10_000) {
    throw new Error(`staged asset is missing or truncated: ${file.to}`);
  }
}

// 3 · The composition tree must bundle, including code-scene.tsx and its runtime.
await bundle({entryPoint: path.join(root, "src", "index.tsx"), publicDir});
fs.rmSync(publicDir, {recursive: true, force: true});
console.log(`engine ok: ${VENDOR_FILES.length} vendored asset(s), compositions bundle`);
