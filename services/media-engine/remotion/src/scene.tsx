/**
 * A single storyboard scene.
 *
 * Composition of the pieces every template shares:
 *   backdrop (still with Ken Burns, or a video clip)
 *   → template frame (the template decides how to present the scene)
 *   → visual elements (badges, scrapbook cut-outs, HUD, arrows, progress)
 *   → captions
 *   → audio (narration + scene sound effects)
 *
 * The template supplies the frame; the scene owns timing so templates cannot drift from the
 * timeline the API computed.
 */

import type React from "react";
import {AbsoluteFill, Audio, Img, OffthreadVideo, Sequence, staticFile, useCurrentFrame} from "remotion";

import {BrandMark, Captions, ProgressBar} from "./captions";
import {CODE_RUNTIME_VERSION} from "./code-runtime";
import {CodeScene} from "./code-scene";
import {GenerativeScene} from "./generative";
import {MONO, kenBurns} from "./motion";
import type {CaptionStyle, StoryboardScene, VideoSpec, VisualElement} from "./types";

export function Backdrop({
  spec,
  scene,
  frame,
  frames,
}: {
  spec: VideoSpec;
  scene: StoryboardScene;
  frame: number;
  frames: number;
}) {
  if (scene.videoSrc) {
    return (
      <AbsoluteFill>
        <OffthreadVideo
          src={staticFile(scene.videoSrc)}
          muted
          style={{width: "100%", height: "100%", objectFit: "cover"}}
        />
      </AbsoluteFill>
    );
  }
  if (scene.mode === "code") {
    // LLM-authored scene. Only a complete, current-runtime scene is used; anything else falls
    // through to the generative backdrop so a bad render stays visible instead of going black.
    if (scene.code?.runtime === CODE_RUNTIME_VERSION && scene.code.html.trim().length > 0) {
      return (
        <AbsoluteFill>
          <CodeScene code={scene.code} accent={scene.accentColor ?? spec.accent} />
        </AbsoluteFill>
      );
    }
    return (
      <AbsoluteFill>
        <GenerativeScene
          prompt={scene.prompt ?? scene.title ?? ""}
          title={scene.title}
          frames={frames}
          accentOverride={scene.accentColor ?? undefined}
          seed={`${scene.id}:${scene.prompt ?? ""}`}
        />
      </AbsoluteFill>
    );
  }
  if (!scene.imageSrc) {
    // Generative mode: the scene is composed from its prompt instead of an image asset. A missing
    // image in an asset-mode scene keeps the explicit "unavailable" notice so failures stay visible.
    if (scene.mode === "generative" || scene.prompt) {
      return (
        <AbsoluteFill>
          <GenerativeScene
            prompt={scene.prompt ?? scene.title ?? ""}
            title={scene.title}
            frames={frames}
            accentOverride={scene.accentColor ?? undefined}
            seed={`${scene.id}:${scene.prompt ?? ""}`}
          />
        </AbsoluteFill>
      );
    }
    return (
      <AbsoluteFill style={{backgroundColor: spec.background}}>
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: MONO,
            fontSize: "1.6cqw",
            letterSpacing: "0.2em",
            textTransform: "uppercase",
            color: spec.ink,
            opacity: 0.35,
          }}
        >
          scene {String(scene.index).padStart(2, "0")} · image unavailable
        </div>
      </AbsoluteFill>
    );
  }
  const transform = kenBurns(scene.animation?.kenBurns ?? "in", frame, frames);
  return (
    <AbsoluteFill style={{overflow: "hidden"}}>
      <Img
        src={staticFile(scene.imageSrc)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform,
          transformOrigin: "50% 50%",
        }}
      />
    </AbsoluteFill>
  );
}

/**
 * Narration and sound effects are attached by the engine (Video.tsx) at absolute composition
 * positions, so a transition never fades the voice.
 */
export function SceneAudio({
  scene,
  fps,
  sceneFrom = 0,
}: {
  scene: StoryboardScene;
  fps: number;
  sceneFrom?: number;
}) {
  return (
    <>
      {scene.narrationSrc ? (
        <Sequence from={sceneFrom} layout="none" name={`narration-${scene.index}`}>
          <Audio src={scene.narrationSrc} volume={1} />
        </Sequence>
      ) : null}
      {(scene.sfx ?? []).map((sfx, i) => (
        <Sequence
          key={`sfx-${i}`}
          from={sceneFrom + Math.max(0, Math.round(sfx.atS * fps))}
          layout="none"
          name={`sfx-${scene.index}-${i}`}
        >
          <Audio src={sfx.src} volume={sfx.volume ?? 0.6} />
        </Sequence>
      ))}
    </>
  );
}

function VisualElements({
  elements,
  accent,
  frame,
  frames,
}: {
  elements: VisualElement[];
  accent: string;
  frame: number;
  frames: number;
}) {
  const appear = Math.min(1, frame / 12);
  const progress = frames > 1 ? frame / frames : 0;
  return (
    <AbsoluteFill style={{pointerEvents: "none"}}>
      {elements.map((el, i) => {
        switch (el.kind) {
          case "progress":
            return <ProgressBar key={i} progress={progress} accent={accent} position={el.position ?? "bottom"} />;
          case "badge": {
            const pos = el.position ?? "top-left";
            return (
              <div
                key={i}
                style={{
                  position: "absolute",
                  left: pos === "top-left" ? "3.6%" : pos === "bottom-left" ? "3.6%" : undefined,
                  right: pos === "top-right" ? "3.6%" : undefined,
                  top: pos === "top-left" || pos === "top-right" ? "3.8%" : undefined,
                  bottom: pos === "bottom-left" ? "4%" : undefined,
                  border: "1px solid #111111",
                  padding: "4px 10px",
                  fontFamily: MONO,
                  fontSize: "1cqw",
                  letterSpacing: "0.14em",
                  textTransform: "uppercase",
                  backgroundColor: "rgba(255,255,255,0.9)",
                  color: "#111111",
                  opacity: appear,
                }}
              >
                {el.text}
              </div>
            );
          }
          case "hud":
            return (
              <div
                key={i}
                style={{
                  position: "absolute",
                  left: el.position === "left" ? "4%" : undefined,
                  right: el.position === "right" ? "4%" : undefined,
                  top: "46%",
                  display: "flex",
                  flexDirection: "column",
                  gap: 2,
                  opacity: appear,
                }}
              >
                <span style={{fontFamily: MONO, fontSize: "0.95cqw", letterSpacing: "0.16em", textTransform: "uppercase", color: "#FFFFFF", opacity: 0.8}}>
                  {el.label}
                </span>
                <span style={{fontFamily: MONO, fontSize: "2.1cqw", color: "#FFFFFF"}}>{el.value}</span>
              </div>
            );
          case "scrapbook":
            return (
              <div
                key={i}
                style={{
                  position: "absolute",
                  left: `${el.x}%`,
                  top: `${el.y}%`,
                  width: `${el.width}%`,
                  transform: `rotate(${el.rotate ?? -3}deg)`,
                  padding: 10,
                  backgroundColor: "#FFFFFF",
                  boxShadow: "0 12px 40px rgba(0,0,0,0.22)",
                  opacity: appear,
                }}
              >
                <Img src={staticFile(el.src)} style={{width: "100%", display: "block"}} />
              </div>
            );
          case "arrow":
            return <Arrow key={i} element={el} accent={accent} appear={appear} />;
          default:
            return null;
        }
      })}
    </AbsoluteFill>
  );
}

function Arrow({
  element,
  accent,
  appear,
}: {
  element: Extract<VisualElement, {kind: "arrow"}>;
  accent: string;
  appear: number;
}) {
  const [x1, y1] = element.from;
  const [x2, y2] = element.to;
  return (
    <svg
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      style={{position: "absolute", inset: 0, width: "100%", height: "100%", opacity: appear}}
    >
      <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={accent} strokeWidth={0.6} />
      <circle cx={x2} cy={y2} r={1} fill={accent} />
      {element.label ? (
        <text x={x2 + 1.5} y={y2 - 1.5} fill="#FFFFFF" fontSize={2.2} fontFamily="monospace">
          {element.label}
        </text>
      ) : null}
    </svg>
  );
}

/**
 * Scene frame used by every template: elements, brand, captions on top of the template's look.
 * `children` is whatever the template laid out underneath.
 */
export function SceneFrame({
  spec,
  scene,
  frames,
  captionStyle,
  showBrand = true,
  children,
}: {
  spec: VideoSpec;
  scene: StoryboardScene;
  frames: number;
  captionStyle: Exclude<CaptionStyle, "none">;
  showBrand?: boolean;
  children?: React.ReactNode;
}) {
  const frame = useCurrentFrame();
  const accent = scene.accentColor || spec.accent;
  return (
    <AbsoluteFill>
      {children}
      <VisualElements elements={scene.elements ?? []} accent={accent} frame={frame} frames={frames} />
      {showBrand ? <BrandMark brand={spec.brand} accent={accent} /> : null}
      {spec.captions.enabled ? (
        <Captions scene={scene} style={captionStyle} localFrame={frame} frames={frames} accent={accent} />
      ) : null}
    </AbsoluteFill>
  );
}
