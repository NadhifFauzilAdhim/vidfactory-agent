/**
 * Static assets the compositions load from the public dir.
 *
 * `render.mjs` stages these for real renders; the Studio needs them in `public/` to preview the
 * same code. Keeping the list in one place means a new engine dependency (here: GSAP for code
 * scenes) cannot be missing in one path and present in the other.
 */

import fs from "node:fs";
import path from "node:path";

/** Package-relative files copied into `<publicDir>/vendor/<name>`. */
export const VENDOR_FILES = [{from: "gsap/dist/gsap.min.js", to: "vendor/gsap.min.js"}];

/**
 * Copy vendor files into `publicDir`. Missing packages throw: a render must never silently produce
 * scenes without their animation library.
 */
export function stageVendor(publicDir, projectRoot) {
  for (const file of VENDOR_FILES) {
    const source = path.join(projectRoot, "node_modules", file.from);
    if (!fs.existsSync(source)) {
      throw new Error(`vendor file missing: ${file.from} (run npm install in services/media-engine/remotion)`);
    }
    const dest = path.join(publicDir, file.to);
    fs.mkdirSync(path.dirname(dest), {recursive: true});
    fs.copyFileSync(source, dest);
  }
}
