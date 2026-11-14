"""Scene code validation: the gate between an LLM answer and a browser that will run it."""

from __future__ import annotations

import pytest

from app.services.scene_code import (
    CODE_RUNTIME_VERSION,
    is_renderable,
    normalise_scene_code,
    strip_wrappers,
    validate_scene_code,
)

GOOD_JS = """
const tl = gsap.timeline({paused: true});
tl.from(".title", {opacity: 0, y: 20, duration: 0.8});
tl.to(".title", {y: -6, duration: 2.4, repeat: -1, yoyo: true, ease: "sine.inOut"}, 1);
window.__timelines.push(tl);
"""

HTML = '<div class="title">Harbour at dusk</div><svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="3"/></svg>'
CSS = ".title{font:italic 5vw Georgia,serif;color:#f7f3ea}"


def _codes(html: str = HTML, css: str = CSS, js: str = GOOD_JS) -> set[str]:
    return {issue.code for issue in validate_scene_code(html, css, js)}


def test_a_well_formed_scene_passes():
    assert validate_scene_code(HTML, CSS, GOOD_JS) == []


def test_document_wrappers_are_stripped_not_rejected():
    wrapped = "<!doctype html><html><head><style>.a{}</style></head><body><div class='x'></div></body></html>"
    html, css, js = strip_wrappers(wrapped, "<style>.a{color:red}</style>", "<script>window.__timelines.push(tl)</script>")
    assert html == "<div class='x'></div>"
    assert css == ".a{color:red}"
    assert js == "window.__timelines.push(tl)"


@pytest.mark.parametrize(
    ("js", "code"),
    [
        ("fetch('/x'); window.__timelines.push(tl);", "network"),
        ("const d = new Date(); window.__timelines.push(tl);", "determinism"),
        ("Math.random(); window.__timelines.push(tl);", "determinism"),
        ("setTimeout(() => {}, 10); window.__timelines.push(tl);", "determinism"),
        ("requestAnimationFrame(() => {}); window.__timelines.push(tl);", "determinism"),
        ("eval('1'); window.__timelines.push(tl);", "escape"),
        ("new Function('1'); window.__timelines.push(tl);", "escape"),
        ("localStorage.setItem('a','b'); window.__timelines.push(tl);", "escape"),
        ("parent.document.body.remove(); window.__timelines.push(tl);", "escape"),
        ("window.__seek = () => {}; window.__timelines.push(tl);", "escape"),
        ("const tl = gsap.timeline({paused: true});", "missing_timeline"),
    ],
)
def test_javascript_violations_are_rejected(js, code):
    assert code in _codes(js=js)


@pytest.mark.parametrize(
    "html",
    [
        '<img src="data:image/png;base64,AA">',
        '<link href="x.css">',
        '<iframe src="about:blank"></iframe>',
        "<title>x</title>",
    ],
)
def test_markup_violations_are_rejected(html):
    assert _codes(html=html)


def test_external_urls_are_rejected_but_data_uris_are_allowed():
    assert "network" in _codes(css=".a{background:url(https://x.test/a.png)}")
    assert "network" in _codes(css=".a{background:url('/local.png')}")
    assert "network" not in _codes(css=".a{background:url(data:image/svg+xml;utf8,<svg/>)}")


@pytest.mark.parametrize(
    "css",
    [
        ".a{background:url(data:image/png;base64,AA)}",
        '.a{background:url("data:image/png;base64,AA")}',
        ".a{background:url( data:image/svg+xml;base64,x)}",
        ".a{fill:url(#grad)}",
        ".a{mask:url(#mask1)}",
        ".a{filter:url( #blur )}",
    ],
)
def test_self_contained_urls_are_allowed(css):
    """Regression: an SVG paint server reference (url(#id)) is not a network fetch."""
    assert "network" not in _codes(css=css)


def test_url_error_names_the_offending_target():
    issues = validate_scene_code(HTML, css=".a{background:url(https://cdn.test/a.png)}", js=GOOD_JS)
    messages = " ".join(str(i) for i in issues)
    assert "cdn.test/a.png" in messages


def test_empty_scene_is_rejected():
    assert "html_empty" in _codes(html="   ")


def test_size_budget_is_enforced():
    assert "html_too_long" in _codes(html="<div>" + "a" * 25_000 + "</div>")


def test_every_problem_is_reported_at_once():
    """The repair round should not need one request per mistake."""
    codes = _codes(html="<img src='x'>", js="fetch('/a');")
    assert {"network", "missing_timeline"} <= codes


def test_a_scene_without_looping_motion_is_flagged():
    """Entrance-only scenes look like a still image once the fade ends — the reported bug."""
    entrance_only = (
        "const tl = gsap.timeline({paused: true});"
        "tl.from('.arm', {y: 20, opacity: 0, duration: 0.8});"
        "window.__timelines.push(tl);"
    )
    assert "missing_loop" in _codes(js=entrance_only)


def test_a_looping_scene_passes():
    looping = (
        "const tl = gsap.timeline({paused: true});"
        "tl.to('.arm', {rotation: 12, duration: 0.9, repeat: -1, yoyo: true, ease: 'sine.inOut'});"
        "window.__timelines.push(tl);"
    )
    assert validate_scene_code(HTML, CSS, looping) == []


def test_loop_detection_accepts_spacing_variants():
    spaced = "window.__timelines.push(gsap.timeline({paused:true}).to('.a',{x:1,repeat : -1}))"
    assert "missing_loop" not in _codes(js=spaced)


def test_normalise_returns_the_storable_shape():
    code = normalise_scene_code("<body><div class='a'></div></body>", "<style>.a{}</style>", GOOD_JS)
    assert code.runtime == CODE_RUNTIME_VERSION
    assert code.has_motion is True
    payload = code.to_payload()
    assert payload["html"] == "<div class='a'></div>"
    assert payload["css"] == ".a{}"
    assert payload["runtime"] == CODE_RUNTIME_VERSION


def test_is_renderable_guards_the_render_path():
    assert is_renderable({"runtime": CODE_RUNTIME_VERSION, "html": "<div>x</div>"})
    assert not is_renderable(None)
    assert not is_renderable({"runtime": 99, "html": "<div>x</div>"})
    assert not is_renderable({"runtime": CODE_RUNTIME_VERSION, "html": "  "})
    assert not is_renderable({"runtime": CODE_RUNTIME_VERSION, "html": 42})
