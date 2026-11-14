/**
 * Code scene runtime — the sandbox shell an LLM-authored scene renders inside.
 *
 * The contract (see docs/11-code-scenes.md):
 *
 *   html : body markup for the stage (inline SVG/CSS only, no external assets)
 *   css  : stylesheet, scoped to the iframe document
 *   js   : optional GSAP code that must register exactly one PAUSED timeline
 *
 * Nothing here is authored by the model. The seek runtime below is the only thing allowed to move
 * time: every frame asks for `__seek(frame, fps)` and each registered timeline jumps to that
 * moment synchronously, so a render is frame-accurate instead of wall-clock driven.
 *
 * The shell is written with `document.write()` from the parent (Chromium allows it for a
 * same-origin `about:blank` iframe), which keeps `srcdoc` out of the React tree and lets the
 * component mount the document exactly once per scene.
 */

export const CODE_RUNTIME_VERSION = 1;

/**
 * Runs *before* the authored script, while `document.write()` is still streaming: it only creates
 * the contract surface (`__timelines`, `__codeErrors`, error trap). `document.write` executes inline
 * scripts synchronously, so this must precede the scene's GSAP code or the push target is missing.
 */
const BOOT_RUNTIME = `
window.__timelines = [];
window.__frameFns = [];
window.__codeErrors = [];
window.addEventListener("error", function (e) {
  window.__codeErrors.push(String((e && e.message) || e));
});
`;

/** Runs *after* the authored script: exposes the synchronous seek used for every rendered frame. */
const SEEK_RUNTIME = `
window.__seek = function (frame, fps) {
  var t = frame / Math.max(1, fps);
  var root = document.documentElement;
  root.style.setProperty("--frame", String(frame));
  root.style.setProperty("--t", t + "s");
  root.style.setProperty("--seconds", String(t));
  for (var i = 0; i < window.__timelines.length; i++) {
    var tl = window.__timelines[i];
    if (tl && typeof tl.pause === "function") tl.pause().seek(t, true);
  }
  /* Per-frame hooks: any motion a timeline cannot express (per-particle maths, canvas drawing,
     path interpolation) can be a function of scene time. Deterministic by construction because it
     only receives the scene time, never a clock. */
  for (var j = 0; j < window.__frameFns.length; j++) {
    var fn = window.__frameFns[j];
    if (typeof fn === "function") {
      try { fn(t, frame); } catch (e) { window.__codeErrors.push(String(e)); }
    }
  }
};
/* The frame is delivered by message, not by a property access: the iframe has an opaque origin
   (no allow-same-origin), so the parent cannot read or call into this window. */
window.addEventListener("message", function (event) {
  var data = event.data;
  if (data && data.type === "factory:seek") window.__seek(data.frame, data.fps);
});
window.__seek(0, 30);
window.__ready = true;
`;

const STAGE_CSS = `
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;height:100%;overflow:hidden;background:transparent}
body{font-family:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
#stage{position:relative;width:100vw;height:100vh;overflow:hidden}
img,svg{display:block;max-width:100%}
`;

/**
 * Neutralise anything that would let authored markup escape the stage: model output is treated as
 * untrusted, so the shell strips document-level tags and script tags before injection. The
 * validator (API side) is the first gate; this is the last one.
 */
export function stripDocumentWrapper(html: string): string {
  return html
    .replace(/<!doctype[^>]*>/gi, "")
    .replace(/<\/?html[^>]*>/gi, "")
    .replace(/<head[\s\S]*?<\/head>/gi, "")
    .replace(/<\/?body[^>]*>/gi, "")
    .replace(/<\/?script[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/<script[^>]*\/?>/gi, "")
    .trim();
}

export function stripStyleWrapper(css: string): string {
  return css
    .replace(/<\/?style[^>]*>/gi, "")
    .replace(/@import[^;]+;/gi, "")
    .trim();
}

/** Keep the authored js but refuse script tags and a redefinition of the seek hooks. */
export function sanitiseScript(js: string | null | undefined): string {
  if (!js) return "";
  return js
    .replace(/<\/?script[^>]*>/gi, "")
    .replace(/window\.__seek\s*=/g, "window.__seekIgnored =")
    .replace(/window\.__ready\s*=/g, "window.__readyIgnored =")
    .trim();
}

export type ShellOptions = {
  html: string;
  css?: string | null;
  js?: string | null;
  /** Inline GSAP build; injected as an inline script so nothing hits the network. */
  gsapSource?: string | null;
  /** Template accent, exposed to the scene as `--accent` so code follows the channel palette. */
  accent?: string;
};

/** Build the complete iframe document for one scene. */
export function buildShell({html, css, js, gsapSource, accent}: ShellOptions): string {
  const body = stripDocumentWrapper(html);
  const style = stripStyleWrapper(css ?? "");
  const script = sanitiseScript(js);
  const scene = script
    ? `try{${script}}catch(e){window.__codeErrors.push(String(e&&e.message||e))}`
    : "";
  const accentCss = accent ? `:root{--accent:${accent}}` : "";
  return `<!doctype html><html><head><meta charset="utf-8"><style>${STAGE_CSS}</style><style>${accentCss}</style><style>${style}</style></head><body><div id="stage">${body}</div><script>${BOOT_RUNTIME}</script>${
    gsapSource ? `<script>${gsapSource}</script>` : ""
  }<script>${scene}</script><script>${SEEK_RUNTIME}</script></body></html>`;
}
