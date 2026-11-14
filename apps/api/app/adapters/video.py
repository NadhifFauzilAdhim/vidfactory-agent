"""Remotion renderer adapter.

The API never encodes video itself. It stages assets, writes a render job description and runs the
project's Remotion CLI (`services/media-engine/remotion/render.mjs`), which calls
`bundle()` → `selectComposition()` → `renderMedia()` from `@remotion/renderer`. This adapter only:

* locates the Remotion project and Node runtime,
* stages the referenced assets into a temporary public directory,
* streams JSON progress events from the child process,
* enforces a wall-clock timeout and a cancellable sentinel file,
* validates the produced MP4 with ffprobe (read-only inspection — no transcode, no muxing),
* cleans up temporary files.

FFmpeg is not invoked for composition: it is Remotion's internal encoder.
"""

# Temporary-directory bookkeeping (staging, sentinel files, output size) is deliberately
# synchronous: these paths are local temp files and the work is a handful of stat() calls.
# ruff: noqa: ASYNC240

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.media import ffprobe_available
from app.core.logging import get_logger
from app.core.settings import REPO_ROOT, get_settings
from app.ports.interfaces import (
    RenderArtifact,
    RenderOutput,
    RenderProgress,
    StoryboardRenderSpec,
)

log = get_logger("factory.remotion")

ProgressCB = Callable[[RenderProgress], Awaitable[None]] | None

#: Node minor versions Remotion 4 supports.
MIN_NODE_MAJOR = 18


class RenderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "render_failed") -> None:
        super().__init__(message)
        self.code = code


class RenderCancelled(RenderError):
    def __init__(self, message: str = "Render cancelled") -> None:
        super().__init__(message, code="cancelled")


def remotion_dir() -> Path:
    settings = get_settings()
    if settings.remotion_dir:
        p = Path(settings.remotion_dir)
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()
    return (REPO_ROOT / "services" / "media-engine" / "remotion").resolve()


@dataclass(slots=True)
class EngineHealth:
    ok: bool
    detail: str
    node_version: str | None = None
    remotion_version: str | None = None
    bundler_installed: bool = False


def _run_command(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def engine_health() -> EngineHealth:
    settings = get_settings()
    directory = remotion_dir()
    code, out, err = _run_command([settings.node_path, "--version"])
    if code != 0:
        return EngineHealth(False, f"Node.js not found ({settings.node_path}): {err or out}")
    version = out.lstrip("v")
    major = int(version.split(".")[0]) if version[:1].isdigit() else 0
    if major < MIN_NODE_MAJOR:
        return EngineHealth(False, f"Node {version} is too old; Remotion needs {MIN_NODE_MAJOR}+")
    if not (directory / "render.mjs").exists():
        return EngineHealth(False, f"Remotion project not found at {directory}", version)
    bundler = directory / "node_modules" / "@remotion" / "bundler"
    if not bundler.exists():
        return EngineHealth(
            False,
            "Dependencies missing — run `npm install` in services/media-engine/remotion",
            version,
            bundler_installed=False,
        )
    package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
    engines = package.get("dependencies", {})
    return EngineHealth(True, "Remotion engine ready", version, engines.get("remotion"), True)


class RemotionRenderer:
    """Runs one render job at a time in this process; concurrency is configured per job."""

    name = "remotion"

    def __init__(self, storage: Any, *, workdir: Path | None = None) -> None:
        self.storage = storage
        self._workdir = workdir
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    # ------------------------------------------------------------------ health
    async def health(self) -> tuple[bool, str]:
        health = await asyncio.to_thread(engine_health)
        if health.ok:
            return True, f"Remotion {health.remotion_version} · Node {health.node_version}"
        return False, health.detail

    @property
    def running(self) -> set[str]:
        return {k for k, p in self._procs.items() if p.returncode is None}

    async def cancel(self, token: str) -> bool:
        proc = self._procs.get(token)
        if proc is None or proc.returncode is not None:
            return False
        # The CLI polls a sentinel file and calls Remotion's cancel signal; SIGTERM is a fallback.
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except TimeoutError:
            proc.kill()
        return True

    # ------------------------------------------------------------------ render
    async def render(
        self,
        spec: StoryboardRenderSpec,
        *,
        on_progress: ProgressCB = None,
        file_path: Callable[[str], Path] | None = None,
    ) -> tuple[RenderArtifact, dict[str, Any]]:
        """Render one job. Returns the artifact plus the composition metadata Remotion reported."""
        settings = get_settings()
        health = await asyncio.to_thread(engine_health)
        if not health.ok:
            raise RenderError(health.detail, code="engine_unavailable")

        work = Path(tempfile.mkdtemp(prefix="factory-remotion-", dir=self._workdir))
        public = work / "public"
        out = work / ("preview.mp4" if not spec.stills_only else "unused.mp4")
        job_path = work / "job.json"
        cancel_file = work / "cancel.flag"
        artifacts = Path(tempfile.mkdtemp(prefix="factory-artifacts-"))
        stills = [dict(s, path=str(work / f"still-{i}.jpg")) for i, s in enumerate(spec.stills)]
        try:
            assets: list[dict[str, str]] = []
            for asset in spec.assets:
                source = Path(asset["source"])
                if file_path is not None:
                    source = file_path(asset["source"])
                if not await asyncio.to_thread(source.exists):
                    raise RenderError(f"Missing asset {asset['source']}", code="missing_asset")
                assets.append({**asset, "source": str(source)})
            payload = {
                "renderId": spec.render_id,
                "compositionId": spec.composition_id,
                "entryPoint": str(remotion_dir() / "src" / "index.tsx"),
                "publicDir": str(public),
                "outputPath": str(out),
                "cancelFile": str(cancel_file),
                "concurrency": spec.concurrency or settings.remotion_concurrency or None,
                "codec": "h264",
                "crf": settings.remotion_crf,
                "jpegQuality": settings.remotion_jpeg_quality,
                "gl": settings.remotion_gl,
                "stills": stills,
                "stillsOnly": spec.stills_only,
                "assets": assets,
                "inputProps": {"spec": spec.spec},
            }
            if any(
                (scene or {}).get("mode") == "code" for scene in (spec.spec.get("scenes") or [])
            ):
                # A code scene mounts a nested document; the default frame timeout is too tight.
                payload["frameTimeoutMs"] = 120000
            job_path.write_text(json.dumps(payload), encoding="utf-8")
            artifact, meta = await self._run_cli(spec, job_path, cancel_file, out, on_progress)
            if spec.stills_only:
                return artifact, meta
            # The staging directory is deleted on the way out, so the produced file (and its
            # preview stills) move to a directory the caller owns and cleans up.
            kept = artifacts / "output.mp4"
            await asyncio.to_thread(shutil.copyfile, out, kept)
            kept_stills: list[str] = []
            for i, _ in enumerate(spec.stills):
                src = work / f"still-{i}.jpg"
                if await asyncio.to_thread(src.exists):
                    dst = artifacts / f"still-{i}.jpg"
                    await asyncio.to_thread(shutil.copyfile, src, dst)
                    kept_stills.append(str(dst))
            if kept_stills:
                meta["stills"] = kept_stills
            return RenderArtifact(
                output_path=str(kept), bytes=artifact.bytes, elapsed_s=artifact.elapsed_s
            ), meta
        finally:
            shutil.rmtree(work, ignore_errors=True)
    async def _run_cli(
        self,
        spec: StoryboardRenderSpec,
        job_path: Path,
        cancel_file: Path,
        out: Path,
        on_progress: ProgressCB,
    ) -> tuple[RenderArtifact, dict[str, Any]]:
        settings = get_settings()
        env = {**os.environ, "REMOTION_LOG_LEVEL": "warn"}
        if settings.remotion_license_key:
            env["REMOTION_LICENSE_KEY"] = settings.remotion_license_key
        proc = await asyncio.create_subprocess_exec(
            settings.node_path,
            str(remotion_dir() / "render.mjs"),
            "--job",
            str(job_path),
            cwd=str(remotion_dir()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        token = spec.cancel_token or spec.render_id
        self._procs[token] = proc
        started = time.perf_counter()
        meta: dict[str, Any] = {}
        error: str | None = None
        cancelled = False
        stderr_tail = ""
        deadline = started + settings.remotion_render_timeout_s

        async def read_stdout() -> None:
            nonlocal error, cancelled
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("remotion output: %s", line[:300])
                    continue
                kind = event.get("type")
                if kind == "composition":
                    meta["composition"] = event
                elif kind == "start":
                    meta["frame_count"] = event.get("frameCount")
                    meta["concurrency"] = event.get("concurrency")
                    if on_progress:
                        await on_progress(RenderProgress(
                            stage="rendering", progress=0.02,
                            total_frames=int(event.get("frameCount") or 0),
                        ))
                elif kind == "progress":
                    if on_progress:
                        await on_progress(RenderProgress(
                            stage=event.get("stage") or "rendering",
                            progress=float(event.get("progress") or 0),
                            rendered_frames=int(event.get("renderedFrames") or 0),
                            total_frames=int(meta.get("frame_count") or 0),
                        ))
                elif kind == "still":
                    meta.setdefault("stills", []).append(event.get("path"))
                elif kind == "done":
                    meta["done"] = event
                elif kind == "error":
                    error = event.get("message") or "render failed"
                elif kind == "cancelled":
                    cancelled = True

        async def read_stderr() -> None:
            nonlocal stderr_tail
            assert proc.stderr is not None
            async for raw in proc.stderr:
                text = raw.decode(errors="replace").strip()
                if text:
                    stderr_tail = (stderr_tail + " | " + text)[-1200:]
                    log.debug("remotion stderr: %s", text[:400])

        readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
        try:
            remaining = max(5.0, deadline - time.perf_counter())
            await asyncio.wait_for(proc.wait(), timeout=remaining)
        except TimeoutError:
            cancel_file.touch()
            proc.kill()
            raise RenderError(
                f"Render exceeded {settings.remotion_render_timeout_s}s and was stopped",
                code="render_timeout",
            ) from None
        finally:
            await asyncio.gather(*readers, return_exceptions=True)
            self._procs.pop(token, None)

        elapsed = time.perf_counter() - started
        if cancelled or proc.returncode == 0 and cancel_file.exists():
            raise RenderCancelled()
        if proc.returncode != 0 or error:
            raise RenderError(
                _shorten(error or f"Remotion exited with code {proc.returncode} {stderr_tail}")
            )
        if spec.stills_only:
            return RenderArtifact(output_path="", bytes=0, elapsed_s=elapsed), meta
        if not out.exists() or out.stat().st_size < 1024:
            raise RenderError("Renderer produced no output file")
        return RenderArtifact(output_path=str(out), bytes=out.stat().st_size, elapsed_s=elapsed), meta

    # ------------------------------------------------------------------ validation
    async def validate(
        self, path: Path, *, expected: dict[str, Any] | None = None
    ) -> tuple[bool, list[dict[str, Any]], list[str], dict[str, Any]]:
        """Inspect the produced MP4 with ffprobe (read-only) and compare with expectations."""
        expected = expected or {}
        probe = await asyncio.to_thread(_probe, path)
        if probe is None:
            return False, [{"id": "probe", "passed": False, "note": "ffprobe unavailable"}], [
                "Could not inspect the rendered file."
            ], {}

        checks: list[dict[str, Any]] = []
        issues: list[str] = []
        video = probe["video"]

        def check(id_: str, passed: bool, note: str) -> None:
            checks.append({"id": id_, "passed": passed, "note": note})
            if not passed:
                issues.append(note)

        size_ok = path.stat().st_size > 50_000
        check("file_integrity", size_ok, f"{path.stat().st_size / 1024:.0f} KB")
        check(
            "video_stream",
            bool(video),
            f"{video.get('codec_name')} {video.get('width')}x{video.get('height')}" if video else "no video stream",
        )
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        if expected.get("width") and expected.get("height"):
            check(
                "dimensions",
                (w, h) == (expected["width"], expected["height"]),
                f"{w}x{h} (expected {expected['width']}x{expected['height']})",
            )
        fps = _parse_fps(video.get("r_frame_rate")) or float(probe.get("fps") or 0)
        if expected.get("fps"):
            check("frame_rate", abs(fps - expected["fps"]) < 0.01, f"{fps:.2f} fps")
        duration = float(probe["duration"])
        if expected.get("duration_s"):
            expected_d = float(expected["duration_s"])
            check(
                "duration",
                abs(duration - expected_d) <= max(0.75, expected_d * 0.06),
                f"{duration:.2f}s (expected {expected_d:.2f}s)",
            )
        audio = probe["audio"]
        check("audio_stream", bool(audio), audio.get("codec_name", "no audio stream") if audio else "no audio stream")
        if audio and expected.get("duration_s"):
            # AAC priming and muxer rounding make audio a few ms shorter than video; that is normal.
            check(
                "av_sync",
                abs(float(audio.get("duration") or duration) - duration) < 0.35,
                "audio and video streams end together",
            )
        check(
            "faststart",
            bool(probe.get("faststart", True)),
            "moov atom present before media data" if probe.get("faststart") else "not optimised for streaming",
        )
        meta = {
            "width": w,
            "height": h,
            "fps": fps,
            "duration_s": round(duration, 3),
            "bitrate_kbps": round(float(probe.get("bitrate") or 0) / 1000, 1),
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name") if audio else None,
        }
        return all(c["passed"] for c in checks), checks, issues, meta

    # ------------------------------------------------------------------ stills
    async def preview(self, spec: StoryboardRenderSpec, *, on_progress: ProgressCB = None):
        artifact, meta = await self.render(
            StoryboardRenderSpec(
                render_id=spec.render_id,
                project_id=spec.project_id,
                composition_id=spec.composition_id,
                spec=spec.spec,
                assets=spec.assets,
                stills=spec.stills,
                stills_only=True,
                cancel_token=spec.cancel_token,
            ),
            on_progress=on_progress,
        )
        return artifact, meta


def _shorten(message: str, limit: int = 1200) -> str:
    return message.replace("\n", " | ")[:limit]


def _parse_fps(value: str | None) -> float | None:
    if not value or "/" not in value:
        return None
    num, den = value.split("/", 1)
    try:
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def _probe(path: Path) -> dict[str, Any] | None:
    if not ffprobe_available():
        return None
    settings = get_settings()
    code, out, err = _run_command(
        [
            settings.ffprobe_path,
            "-v", "error",
            "-show_entries",
            "format=duration,bit_rate:stream=codec_type,codec_name,width,height,r_frame_rate,duration",
            "-of", "json",
            str(path),
        ],
        timeout=60,
    )
    if code != 0:
        log.warning("ffprobe failed: %s", err[:200])
        return None
    payload = json.loads(out or "{}")
    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt = payload.get("format", {})
    faststart = _has_faststart(path)
    return {
        "duration": float(fmt.get("duration") or video.get("duration") or 0),
        "bitrate": fmt.get("bit_rate"),
        "video": video,
        "audio": audio,
        "faststart": faststart,
        "fps": _parse_fps(video.get("r_frame_rate")),
    }


def _has_faststart(path: Path) -> bool:
    """True when the moov atom precedes mdat (players can start before the file is complete)."""
    try:
        with path.open("rb") as fh:
            offset = 0
            size = path.stat().st_size
            while offset < size and offset < 8 * 1024 * 1024:
                fh.seek(offset)
                header = fh.read(8)
                if len(header) < 8:
                    break
                length = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if length <= 0:
                    break
                offset += length
    except OSError:
        return False
    return False


__all__ = [
    "EngineHealth",
    "MIN_NODE_MAJOR",
    "RemotionRenderer",
    "RenderCancelled",
    "RenderError",
    "RenderOutput",
    "engine_health",
    "remotion_dir",
]
