/**
 * Contract between the Factory API and the Remotion compositions.
 *
 * The API resolves everything that needs a database or storage access (asset keys, narration
 * durations, music selection) and sends this plain, JSON-serialisable storyboard. Compositions
 * stay pure: same props → same frames, which is what makes rendering deterministic and previews
 * faithful to the export.
 */

export type TransitionKind =
  | "fade"
  | "slide-left"
  | "slide-up"
  | "wipe"
  | "iris"
  | "zoom"
  | "cut";

export type EntranceKind = "fade" | "rise" | "slide" | "scale" | "typewriter" | "none";
export type KenBurnsKind = "in" | "out" | "pan-left" | "pan-right" | "none";
export type CaptionStyle = "none" | "lower-third" | "subtitle" | "overlay-title";

export type VisualElement =
  | { kind: "badge"; text: string; position?: "top-left" | "top-right" | "bottom-left" }
  | { kind: "scrapbook"; src: string; x: number; y: number; width: number; rotate?: number }
  | { kind: "hud"; label: string; value: string; position?: "left" | "right" }
  | { kind: "progress"; position?: "top" | "bottom" }
  | { kind: "arrow"; from: [number, number]; to: [number, number]; label?: string };

export type SceneAnimation = {
  kenBurns?: KenBurnsKind;
  entrance?: EntranceKind;
  /** Caption behaviour for this scene. Defaults to the template's caption mode. */
  caption?: CaptionStyle;
  /** Per-scene override of the incoming transition (template default otherwise). */
  transitionIn?: TransitionKind;
  transitionDurationInFrames?: number;
};

/**
 * A scene authored as code by the LLM (HTML + CSS + optional GSAP). Rendered in a sandboxed iframe
 * by `code-scene.tsx`; see `code-runtime.ts` for the seek contract.
 */
export type SceneCode = {
  /** Body markup for the stage. No document-level tags; the runtime supplies them. */
  html: string;
  /** Stylesheet applied inside the iframe document. */
  css?: string | null;
  /** GSAP code that registers paused timelines on `window.__timelines`. */
  js?: string | null;
  /** Contract version; scenes written for another runtime fall back to generative. */
  runtime: 1;
};

export type StoryboardScene = {
  id: string;
  index: number;
  title?: string;
  /**
   * How the scene is drawn:
   *  - "asset" (default) — a generated image or video clip
   *  - "generative" — no image at all: the scene is composed procedurally from the prompt
   *  - "code" — the scene is HTML/CSS/GSAP written by the LLM
   */
  mode?: "asset" | "generative" | "code";
  /** The scene's authored code. Absent means the scene falls back to generative. */
  code?: SceneCode | null;
  /** The visual prompt the storyboard agent wrote (used by generative scenes). */
  prompt?: string | null;
  /** Bundle-relative asset path (staticFile-style), resolved by the render job. */
  imageSrc?: string | null;
  /** Optional video clip; when present it replaces the still image. */
  videoSrc?: string | null;
  narrationSrc?: string | null;
  narrationDurationS?: number | null;
  /** Displayed caption (lower third / subtitle). */
  caption?: string | null;
  /** Optional words for karaoke-style highlighting, absolute seconds within the scene. */
  words?: { text: string; startS: number; endS: number }[];
  durationS: number;
  transition: TransitionKind;
  animation?: SceneAnimation;
  elements?: VisualElement[];
  /** Scene-level sound effect, absolute seconds within the scene and volume 0..1. */
  sfx?: { src: string; atS: number; volume?: number }[];
  accentColor?: string | null;
};

export type MusicSettings = {
  /** Bundle-relative src (staticFile) of the looping bed. */
  url: string | null;
  volume: number;
  /** Duck the bed under narration (recommended for storytelling). */
  duckUnderNarration: boolean;
  /** Fade in/out length in seconds. */
  fadeInS?: number;
  fadeOutS?: number;
};

export type VideoSpec = {
  version: 1;
  title: string;
  brand: string;
  templateId: string;
  width: number;
  height: number;
  fps: number;
  accent: string;
  background: string;
  ink: string;
  /** Multiplier applied to every duration; 1 = as authored. */
  speed?: number;
  captions: {
    enabled: boolean;
    mode: Exclude<CaptionStyle, "none">;
    language?: string;
  };
  intro?: {
    enabled: boolean;
    durationS: number;
    title: string;
    subtitle?: string | null;
  } | null;
  outro?: {
    enabled: boolean;
    durationS: number;
    headline: string;
    subline?: string | null;
    cta?: string | null;
  } | null;
  scenes: StoryboardScene[];
  music: MusicSettings;
  /** Default incoming transition for scenes that do not override it. */
  defaultTransition: TransitionKind;
  transitionDurationS: number;
};

export type StoryboardProps = {
  spec: VideoSpec;
};
