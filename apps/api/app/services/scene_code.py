"""Validation and normalisation for LLM-authored scene code (``visual_mode="code"``).

A code scene is untrusted input that eventually runs as JavaScript inside Chromium. This module is
the gate that runs before the code is stored, and the renderer repeats the cheap parts of it as a
last resort (``services/media-engine/remotion/src/code-runtime.ts``).

It deliberately does **not** try to be a sandbox. The sandbox is the iframe
(``sandbox="allow-scripts"``, opaque origin, no network origin access). What this module guarantees:

* the scene is deterministic — nothing driven by wall clock, timers or randomness;
* the scene is self-contained — no network, no external assets, no package imports;
* the scene cannot escape into the composition or try to intercept the frame seek contract;
* the payload stays small enough to render quickly.

Anything uncertain is rejected, not "cleaned up and hoped for".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CODE_RUNTIME_VERSION = 1
MAX_HTML_CHARS = 24_000
MAX_CSS_CHARS = 16_000
MAX_JS_CHARS = 16_000
MAX_TOTAL_CHARS = 48_000

#: A timeline must exist for anything but a static scene; the renderer seeks it per frame.
_TIMELINE_HINT = re.compile(r"__timelines\s*\.\s*push|__timelines\s*=", re.IGNORECASE)
#: Looping motion: ``repeat: -1`` (with or without yoyo). Entrances alone leave the scene static.
_LOOP_HINT = re.compile(r"repeat\s*:\s*-1", re.IGNORECASE)
#: Per-frame hook: the runtime calls each function with the scene time, every frame.
_FRAME_FN_HINT = re.compile(r"__frameFns\s*\.\s*push|__frameFns\s*=", re.IGNORECASE)


def has_motion(js: str) -> bool:
    """True when the scene keeps moving for its whole duration, however the author chose to do it."""
    return bool(_LOOP_HINT.search(js) or _FRAME_FN_HINT.search(js))

_FORBIDDEN: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # --- network / external resources -------------------------------------------------
    ("network", re.compile(r"\bfetch\s*\(", re.I), "fetch() is not allowed in a scene"),
    ("network", re.compile(r"\bXMLHttpRequest\b", re.I), "XMLHttpRequest is not allowed"),
    ("network", re.compile(r"\bWebSocket\b", re.I), "WebSocket is not allowed"),
    ("network", re.compile(r"\bEventSource\b", re.I), "EventSource is not allowed"),
    ("network", re.compile(r"\bimport\s*\(", re.I), "dynamic import() is not allowed"),
    ("network", re.compile(r"^\s*import\s+[\w{*]", re.I | re.M), "ES module imports are not allowed"),
    ("network", re.compile(r"https?://", re.I), "external URLs are not allowed — use inline SVG/CSS"),
    ("network", re.compile(r"@import", re.I), "@import is not allowed"),
    ("network", re.compile(r"<link\b", re.I), "<link> tags are not allowed"),
    ("network", re.compile(r"<img\b", re.I), "<img> tags are not allowed — use inline SVG"),
    # --- determinism --------------------------------------------------------------------
    ("determinism", re.compile(r"\bMath\s*\.\s*random\b"), "Math.random() makes renders differ run to run"),
    ("determinism", re.compile(r"\bDate\s*\.\s*now\b"), "Date.now() makes renders differ run to run"),
    ("determinism", re.compile(r"\bnew\s+Date\b"), "new Date() makes renders differ run to run"),
    ("determinism", re.compile(r"\bperformance\s*\.\s*now\b"), "performance.now() is wall-clock time"),
    ("determinism", re.compile(r"\brequestAnimationFrame\b"), "requestAnimationFrame is wall-clock driven"),
    ("determinism", re.compile(r"\bsetTimeout\b"), "setTimeout is wall-clock driven"),
    ("determinism", re.compile(r"\bsetInterval\b"), "setInterval is wall-clock driven"),
    # --- sandbox escape attempts ----------------------------------------------------------
    ("escape", re.compile(r"\beval\s*\(", re.I), "eval() is not allowed"),
    ("escape", re.compile(r"\bnew\s+Function\b", re.I), "new Function() is not allowed"),
    ("escape", re.compile(r"\bdocument\s*\.\s*cookie\b", re.I), "document.cookie is not allowed"),
    ("escape", re.compile(r"\b(localStorage|sessionStorage|indexedDB)\b", re.I), "storage APIs are not allowed"),
    ("escape", re.compile(r"\bwindow\s*\.\s*(parent|top|opener)\b|\b(parent|top)\s*\.\s*(document|location)", re.I),
     "reaching outside the scene is not allowed"),
    ("escape", re.compile(r"window\s*\.\s*__seek\s*=", re.I), "the seek runtime is owned by the engine"),
    ("escape", re.compile(r"<\s*(iframe|object|embed|base|meta)\b", re.I), "nested documents are not allowed"),
)

_TITLE_RE = re.compile(r"<title[^>]*>.*?</title>", re.I | re.S)
#: Every `url(...)` reference; the target is inspected separately (see `_external_urls`).
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)


def _external_urls(css: str) -> list[str]:
    """`url()` targets that are not inline data and not an in-document reference.

    ``url(#gradient)`` and ``url(data:image/svg+xml,...)`` are self-contained; everything else
    (http, protocol-relative, or a relative path) would require network or files the render does not
    have, so it is rejected by name instead of by a clever regex.
    """
    found: list[str] = []
    for _quote, target in _CSS_URL_RE.findall(css):
        value = target.strip()
        if not value or value.startswith(("data:", "#")):
            continue
        found.append(value)
    return found


@dataclass(frozen=True, slots=True)
class SceneCodeIssue:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.detail}"


@dataclass(slots=True)
class SceneCode:
    html: str
    css: str = ""
    js: str = ""
    runtime: int = CODE_RUNTIME_VERSION

    @property
    def has_motion(self) -> bool:
        return bool(_TIMELINE_HINT.search(self.js))

    def to_payload(self) -> dict[str, object]:
        return {"html": self.html, "css": self.css, "js": self.js or None, "runtime": self.runtime}


def strip_wrappers(html: str, css: str, js: str) -> tuple[str, str, str]:
    """Remove the document scaffolding models add out of habit.

    Wrappers are noise, not a safety problem: the runtime strips them again. Doing it here keeps the
    stored code small and the validator strict about what the model actually meant to write.
    """
    html = re.sub(r"<!doctype[^>]*>", "", html, flags=re.I)
    html = re.sub(r"</?html[^>]*>|</?body[^>]*>", "", html, flags=re.I)
    html = re.sub(r"<head\b[\s\S]*?</head>", "", html, flags=re.I)
    html = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", html, flags=re.I)
    html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html, flags=re.I)
    js = re.sub(r"</?script[^>]*>", "", js, flags=re.I)
    js = re.sub(r"^\s*(export\s+default|export)\s+", "", js, flags=re.M)
    css = re.sub(r"</?style[^>]*>", "", css, flags=re.I)
    return html.strip(), css.strip(), js.strip()


def validate_scene_code(html: str, css: str = "", js: str = "") -> list[SceneCodeIssue]:
    """Return every problem with a scene, so the repair round gets all of them at once.

    ``require_motion`` keeps the quality gate that stops entrance-only scenes. It is *not* a rule
    about how motion must be written: any of these count as motion:

    * ``repeat: -1`` (a looping tween)
    * ``nested timeline`` (``gsap.timeline({repeat: -1})`` added into the main one)
    * a per-frame hook pushed onto ``window.__frameFns`` (the runtime calls each with the scene time)
    """
    issues: list[SceneCodeIssue] = []
    html, css, js = strip_wrappers(html or "", css or "", js or "")

    if not html:
        issues.append(SceneCodeIssue("html_empty", "the scene has no markup"))
    if len(html) > MAX_HTML_CHARS:
        issues.append(SceneCodeIssue("html_too_long", f"html is {len(html)} chars (max {MAX_HTML_CHARS})"))
    if len(css) > MAX_CSS_CHARS:
        issues.append(SceneCodeIssue("css_too_long", f"css is {len(css)} chars (max {MAX_CSS_CHARS})"))
    if len(js) > MAX_JS_CHARS:
        issues.append(SceneCodeIssue("js_too_long", f"js is {len(js)} chars (max {MAX_JS_CHARS})"))
    if len(html) + len(css) + len(js) > MAX_TOTAL_CHARS:
        issues.append(SceneCodeIssue("scene_too_large", "the scene exceeds the total size budget"))

    haystack = {"html": html, "css": css, "js": js}
    for part, text in haystack.items():
        for code, pattern, detail in _FORBIDDEN:
            match = pattern.search(text)
            if match:
                issues.append(SceneCodeIssue(code, f"{part}: {detail} (found {match.group(0)[:40]!r})"))

    # `url()` needs its target inspected, so it is checked outside the regex table.
    for part in ("html", "css"):
        for target in _external_urls(haystack[part]):
            issues.append(
                SceneCodeIssue(
                    "network",
                    f"{part}: url({target[:60]!r}) is external — use a data: URI or an in-document "
                    "#reference",
                )
            )

    if _TITLE_RE.search(html):
        issues.append(SceneCodeIssue("html_no_title", "remove <title>; the scene is not a page"))

    if js and not _TIMELINE_HINT.search(js):
        issues.append(
            SceneCodeIssue(
                "missing_timeline",
                "js must push a paused gsap timeline into window.__timelines, e.g. "
                "window.__timelines.push(gsap.timeline({paused: true})...)",
            )
        )
    elif js and not has_motion(js):
        # Entrance-only scenes look like a still image once the fade finishes. This is the failure
        # the operator reported; the repair round is the cheapest place to fix it. Any deterministic
        # form of continuous motion is accepted (loops, nested loops, per-frame hooks).
        issues.append(
            SceneCodeIssue(
                "missing_loop",
                "the scene goes still after its entrance — keep something moving for the whole "
                "scene with `repeat: -1`, a nested repeating timeline, or a function pushed onto "
                "window.__frameFns that reads the scene time",
            )
        )

    return issues


def normalise_scene_code(html: str, css: str = "", js: str = "") -> SceneCode:
    """Strip wrappers and return the storable scene. Assumes validation already passed."""
    clean_html, clean_css, clean_js = strip_wrappers(html, css, js)
    return SceneCode(html=clean_html, css=clean_css, js=clean_js)


def is_renderable(payload: dict | None) -> bool:
    """True when a stored scene payload can be rendered as authored code."""
    if not isinstance(payload, dict):
        return False
    if payload.get("runtime") != CODE_RUNTIME_VERSION:
        return False
    html = payload.get("html")
    return isinstance(html, str) and bool(html.strip())


@dataclass(frozen=True, slots=True)
class SceneCodeTemplate:
    """Values offered to the prompt so every scene shares the channel's palette and geometry."""

    width: int
    height: int
    fps: int
    duration_s: float
    accent: str
    background: str
    ink: str
    brand: str
