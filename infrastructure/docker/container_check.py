"""Container check: render a 3-scene composition with the engine baked into the image.

Scene 1 and 2 are plain (image-less) scenes; scene 3 is a **code scene** — HTML/CSS/GSAP staged as a
``codeKey`` asset and mounted in the sandboxed iframe. That is the part most likely to break inside
a container (headless Chromium, sandbox flags, GSAP staging), so the smoke test renders it for real
instead of only checking that the engine reports healthy.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from app.adapters.storage import LocalStorage
from app.adapters.video import RemotionRenderer
from app.ports.interfaces import StoryboardRenderSpec
from app.services.scene_code import CODE_RUNTIME_VERSION

SCENE_CODE = {
    "runtime": CODE_RUNTIME_VERSION,
    "html": (
        '<div class="sun"></div><div class="horizon"></div>'
        '<div class="titles"><p class="kicker">container check</p>'
        '<h1 class="headline">Code scene</h1><p class="rule"></p></div>'
    ),
    "css": (
        "body{background:linear-gradient(180deg,#0b1b2a 0%,#2a4a63 68%,#d9b07a 100%)}"
        ".horizon{position:absolute;left:0;right:0;top:64%;height:2px;"
        "background:rgba(255,255,255,.35)}"
        ".sun{position:absolute;left:60%;top:44%;width:14vw;height:14vw;border-radius:50%;"
        "background:radial-gradient(circle,#ffd98a 0%,#f2a65a 60%,transparent 72%)}"
        ".titles{position:absolute;left:7vw;bottom:18vh}"
        ".kicker{font:500 1vw/1.2 ui-monospace,monospace;letter-spacing:.28em;"
        "text-transform:uppercase;color:var(--accent,#d6ff45)}"
        ".headline{margin-top:1.4vh;font:italic 400 5.4vw/1.02 Georgia,serif;color:#f7f3ea}"
        ".rule{margin-top:1.8vh;width:9vw;height:2px;background:var(--accent,#d6ff45);"
        "transform-origin:left center}"
    ),
    "js": (
        "const tl = gsap.timeline({paused: true});"
        'tl.from(".sun", {y: 60, opacity: 0, duration: 1.2, ease: "power2.out"}, 0);'
        'tl.from(".headline", {opacity: 0, y: 26, duration: 0.9, ease: "power3.out"}, 0.7);'
        'tl.fromTo(".rule", {scaleX: 0}, {scaleX: 1, duration: 0.8, ease: "power2.inOut"}, 1.2);'
        "window.__timelines.push(tl);"
    ),
}


async def run() -> int:
    root = Path(tempfile.mkdtemp(prefix="docker-smoke-"))
    renderer = RemotionRenderer(LocalStorage(root / "objects"))
    ok, detail = await renderer.health()
    print(f"engine: {'ok' if ok else 'FAILED'} - {detail}")
    if not ok:
        return 1

    code_file = root / "code-03.json"
    code_file.write_text(json.dumps(SCENE_CODE), encoding="utf-8")

    spec = StoryboardRenderSpec(
        render_id="docker",
        project_id="docker",
        composition_id="youtube-editorial",
        spec={
            "version": 1, "title": "Container check", "brand": "FACTORY",
            "templateId": "youtube-editorial", "width": 960, "height": 540, "fps": 30,
            "accent": "#D6FF45", "background": "#FFFFFF", "ink": "#111111",
            "captions": {"enabled": True, "mode": "lower-third"},
            "defaultTransition": "fade", "transitionDurationS": 0.4,
            "intro": {"enabled": False, "durationS": 0, "title": "Container check"},
            "outro": {"enabled": False, "durationS": 0, "headline": ""},
            "music": {"url": None, "volume": 0.1, "duckUnderNarration": True},
            "scenes": [
                {"id": "s1", "index": 1, "title": "One", "durationS": 1.5,
                 "transition": "fade", "caption": "Rendering works inside the container."},
                {"id": "s2", "index": 2, "title": "Two", "durationS": 1.5,
                 "transition": "fade", "caption": "Captions, audio and encode included."},
                {"id": "s3", "index": 3, "title": "Code", "mode": "code", "codeKey": "scene-3",
                 "prompt": "a harbour at dusk", "durationS": 2.5,
                 "transition": "fade", "caption": "A scene written as HTML, CSS and GSAP."},
            ],
        },
        assets=[{"key": "scene-3", "source": str(code_file), "name": "code-03.json"}],
    )
    artifact, meta = await renderer.render(spec)
    print("composition:", json.dumps(meta.get("composition")))
    print(f"output: {artifact.bytes // 1024} KB in {artifact.elapsed_s:.1f}s")

    passed, checks, issues, info = await renderer.validate(
        Path(artifact.output_path), expected={"width": 960, "height": 540, "fps": 30}
    )
    for check in checks:
        print(f"  [{'x' if check['passed'] else ' '}] {check['id']:<14} {check['note']}")
    print("validation:", "passed" if passed else f"FAILED - {'; '.join(issues)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
