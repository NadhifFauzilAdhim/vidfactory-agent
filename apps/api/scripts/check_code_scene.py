"""Render a code-mode scene through the real renderer path (``codeKey`` → staged JSON → CodeScene).

Run from ``apps/api`` with the API venv:

    .\\.venv\\Scripts\\python.exe scripts\\check_code_scene.py

It stages a scene programme exactly like the pipeline stores it, calls ``RemotionRenderer.render``
(the same code the worker uses) and writes the video plus stills under ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from app.adapters.storage import get_storage
from app.adapters.video import RemotionRenderer
from app.ports.interfaces import StoryboardRenderSpec
from app.services.scene_code import CODE_RUNTIME_VERSION

SCENE_CODE = {
    "runtime": CODE_RUNTIME_VERSION,
    "html": """
      <div class="sun"></div>
      <div class="horizon"></div>
      <div class="city">
        <span class="block b1"></span><span class="block b2"></span><span class="block b3"></span>
      </div>
      <div class="titles">
        <p class="kicker">scene 01</p>
        <h1 class="headline">The harbour at dusk</h1>
        <p class="rule"></p>
      </div>
    """.strip(),
    "css": """
      body { background: linear-gradient(180deg, #0b1b2a 0%, #2a4a63 68%, #d9b07a 100%); }
      .horizon { position:absolute; left:0; right:0; top:64%; height:2px; background:rgba(255,255,255,.35); }
      .sun { position:absolute; left:60%; top:44%; width:14vw; height:14vw; border-radius:50%;
             background: radial-gradient(circle, #ffd98a 0%, #f2a65a 60%, transparent 72%); }
      .city { position:absolute; left:0; right:0; top:50%; height:14%; display:flex; align-items:flex-end; gap:1.2vw; padding:0 6vw; }
      .block { background:#0e1626; border-radius:3px 3px 0 0; width:6vw; }
      .b1{height:42%}.b2{height:74%}.b3{height:56%}
      .titles { position:absolute; left:7vw; bottom:18vh; max-width:44vw; }
      .kicker { font: 500 1vw/1.2 ui-monospace, monospace; letter-spacing:.28em; text-transform:uppercase; color:var(--accent); }
      .headline { margin-top:1.4vh; font: italic 400 5.4vw/1.02 Georgia, serif; color:#f7f3ea; }
      .rule { margin-top:1.8vh; width:9vw; height:2px; background:var(--accent); transform-origin:left center; }
    """.strip(),
    "js": """
      const tl = gsap.timeline({paused: true});
      tl.from(".sun", {y: 60, opacity: 0, duration: 1.2, ease: "power2.out"}, 0);
      tl.from(".block", {y: 90, duration: 0.7, stagger: 0.07, ease: "power3.out"}, 0.2);
      tl.from(".headline", {opacity: 0, y: 26, duration: 0.9, ease: "power3.out"}, 0.7);
      tl.fromTo(".rule", {scaleX: 0}, {scaleX: 1, duration: 0.8, ease: "power2.inOut"}, 1.2);
      window.__timelines.push(tl);
    """.strip(),
}


def build_spec() -> dict:
    return {
        "version": 1,
        "title": "Code scene check",
        "brand": "FACTORY",
        "templateId": "youtube-editorial",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "accent": "#D6FF45",
        "background": "#FFFFFF",
        "ink": "#111111",
        "captions": {"enabled": True, "mode": "lower-third"},
        "defaultTransition": "cut",
        "transitionDurationS": 0.4,
        "intro": {"enabled": False},
        "outro": {"enabled": False},
        "music": {"url": None, "volume": 0.1, "duckUnderNarration": False},
        "scenes": [
            {
                "id": "cs1",
                "index": 1,
                "title": "The harbour at dusk",
                "mode": "code",
                "codeKey": "model-src",
                "prompt": "a harbour at dusk",
                "durationS": 4.0,
                "transition": "cut",
                "caption": "The harbour turned the colour of pale tea.",
            },
            {
                "id": "cs2",
                "index": 2,
                "title": "Unwritten scene",
                "mode": "code",
                "prompt": "a quiet room with a warm lamp",
                "durationS": 2.0,
                "transition": "fade",
                "caption": "A scene without code falls back to generative.",
            },
        ],
    }


async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    code_file = out / "code-01.json"
    code_file.write_text(json.dumps(SCENE_CODE), encoding="utf-8")
    spec = build_spec()
    renderer = RemotionRenderer(get_storage())
    artifact, meta = await renderer.render(
        StoryboardRenderSpec(
            render_id="check-code-scene",
            project_id="check",
            composition_id="youtube-editorial",
            spec=spec,
            assets=[{"key": "model-src", "source": str(code_file), "name": "code-01.json"}],
            stills=[{"frame": 10}, {"frame": 90}],
        )
    )
    target = out / "check-code-scene.mp4"
    shutil.copyfile(artifact.output_path, target)
    for i, still in enumerate(meta.get("stills") or []):
        shutil.copyfile(still, out / f"check-code-still-{i}.jpg")
    print(f"frames: {meta.get('composition', {}).get('durationInFrames')}")
    print(f"video:  {target} ({artifact.bytes // 1024} KB)")
    ok, checks, issues, info = await renderer.validate(
        target,
        expected={
            "width": spec["width"],
            "height": spec["height"],
            "fps": spec["fps"],
            "duration_s": meta.get("composition", {}).get("durationInFrames", 0) / spec["fps"],
        },
    )
    for check in checks:
        print(f"  {'ok ' if check['passed'] else 'FAIL'} {check['id']}: {check['note']}")
    if not ok:
        raise SystemExit(f"validation failed: {issues}")
    print("PASS: code-mode render is a complete MP4")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path("C:/Users/Asus/AppData/Local/Temp/opencode/code-scene")))
    asyncio.run(main(Path(parser.parse_args().out)))
