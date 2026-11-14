/**
 * Composition root.
 *
 * Every template is registered as its own composition sharing the same engine component, so the
 * Studio, the Player and `renderMedia()` all use identical code. `calculateMetadata` derives the
 * duration from the storyboard, which means the API does not have to send a frame count.
 */

import type React from "react";
import {Composition, Folder, registerRoot} from "remotion";

import {CATALOGUE, DEFAULT_TEMPLATE} from "./catalogue";
import {StoryboardVideo} from "./Video";
import type {StoryboardProps} from "./types";

const SAMPLE_HINT = "The video is rendered from storyboard props; open the Data panel to edit them.";

function calculateMetadata({props}: {props: StoryboardProps}) {
  const spec = props.spec;
  const fps = spec.fps || 30;
  const overlap = Math.round((spec.transitionDurationS || 0) * fps);
  const sceneFrames = spec.scenes.reduce((total, scene, i) => {
    const frames = Math.max(12, Math.round((scene.durationS / (spec.speed || 1)) * fps));
    const cuts = i === 0 ? 0 : overlap;
    return total + frames - cuts;
  }, 0);
  const intro = spec.intro?.enabled ? Math.round(spec.intro.durationS * fps) : 0;
  const outro = spec.outro?.enabled ? Math.round(spec.outro.durationS * fps) : 0;
  return {
    durationInFrames: Math.max(12, sceneFrames + intro + outro),
    fps,
    width: spec.width || 1920,
    height: spec.height || 1080,
    props,
  };
}

const ROOT_PROPS: StoryboardProps = {
  spec: {
    version: 1,
    title: "The Lantern Path",
    brand: "FACTORY",
    templateId: DEFAULT_TEMPLATE,
    width: 1920,
    height: 1080,
    fps: 30,
    accent: "#D6FF45",
    background: "#FFFFFF",
    ink: "#111111",
    captions: {enabled: true, mode: "lower-third"},
    defaultTransition: "fade",
    transitionDurationS: 0.6,
    intro: {enabled: true, durationS: 2.4, title: "The Lantern Path", subtitle: "A story for curious children"},
    outro: {
      enabled: true,
      durationS: 3,
      headline: "The end.",
      subline: "Mossy keeps the harbour light.",
      cta: "Subscribe for a new story every week.",
    },
    music: {url: null, volume: 0.12, duckUnderNarration: true, fadeInS: 1.2, fadeOutS: 2},
    scenes: [
      {
        id: "m1",
        index: 1,
        title: SAMPLE_HINT,
        imageSrc: null,
        narrationSrc: null,
        durationS: 4.5,
        transition: "fade",
        caption: "The sun went down behind the hill and the harbour turned the colour of pale tea.",
        animation: {kenBurns: "in", entrance: "rise"},
      },
      {
        id: "m2",
        index: 2,
        imageSrc: null,
        narrationSrc: null,
        durationS: 4,
        transition: "fade",
        caption: "Mossy lit a lantern on the tallest post she could find.",
        animation: {kenBurns: "pan-right", entrance: "rise"},
        elements: [{kind: "progress", position: "bottom"}],
      },
      {
        id: "m3",
        index: 3,
        imageSrc: null,
        narrationSrc: null,
        durationS: 4.2,
        transition: "fade",
        caption: "By midnight, every boat could see the way home.",
        animation: {kenBurns: "out", entrance: "scale"},
      },
      {
        id: "m4",
        index: 4,
        title: "Code scene",
        // Authored visuals: HTML/CSS/GSAP instead of an image. Edit this object in the Studio's Data
        // panel to try your own scene, exactly like the code_scene agent produces one.
        mode: "code",
        prompt: "a harbour at dusk with a lantern",
        code: {
          runtime: 1,
          html: '<div class="sun"></div><div class="horizon"></div><div class="city"><span class="b1"></span><span class="b2"></span><span class="b3"></span></div><div class="titles"><p class="kicker">scene 04</p><h1 class="headline">The lantern lit</h1><p class="rule"></p></div>',
          css:
            'body{background:linear-gradient(180deg,#0b1b2a 0%,#2a4a63 68%,#d9b07a 100%)}' +
            ".horizon{position:absolute;left:0;right:0;top:64%;height:2px;background:rgba(255,255,255,.35)}" +
            ".sun{position:absolute;left:62%;top:44%;width:14vw;height:14vw;border-radius:50%;background:radial-gradient(circle,#ffd98a 0%,#f2a65a 60%,transparent 72%)}" +
            ".city{position:absolute;left:0;right:0;top:50%;height:14%;display:flex;align-items:flex-end;gap:1.2vw;padding:0 6vw}" +
            ".city span{display:block;background:#0e1626;border-radius:3px 3px 0 0;width:6vw}" +
            ".b1{height:42%}.b2{height:74%}.b3{height:56%}" +
            '.titles{position:absolute;left:7vw;bottom:18vh}.kicker{font:500 1vw/1.2 ui-monospace,monospace;letter-spacing:.28em;text-transform:uppercase;color:var(--accent, #d6ff45)}' +
            '.headline{margin-top:1.4vh;font:italic 400 5.4vw/1.02 Georgia,serif;color:#f7f3ea}' +
            '.rule{margin-top:1.8vh;width:9vw;height:2px;background:var(--accent, #d6ff45);transform-origin:left center}',
          js:
            "const tl = gsap.timeline({paused: true});" +
            'tl.from(".sun", {y: 60, opacity: 0, duration: 1.2, ease: "power2.out"}, 0);' +
            'tl.from(".city span", {y: 90, duration: 0.7, stagger: 0.07, ease: "power3.out"}, 0.2);' +
            'tl.from(".headline", {opacity: 0, y: 26, duration: 0.9, ease: "power3.out"}, 0.7);' +
            'tl.fromTo(".rule", {scaleX: 0}, {scaleX: 1, duration: 0.8, ease: "power2.inOut"}, 1.2);' +
            "window.__timelines.push(tl);",
        },
        narrationSrc: null,
        durationS: 4.5,
        transition: "fade",
        caption: "She lit the lantern and the whole harbour answered.",
      },
    ],
  },
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Folder name="Factory">
        {Object.entries(CATALOGUE).map(([id, template]) => (
          <Composition
            key={id}
            id={id}
            component={StoryboardVideo}
            durationInFrames={300}
            fps={template.fps}
            width={template.width}
            height={template.height}
            defaultProps={{
              ...ROOT_PROPS,
              spec: {
                ...ROOT_PROPS.spec,
                templateId: id,
                width: template.width,
                height: template.height,
                fps: template.fps,
                accent: template.colors.accent,
                background: template.colors.background,
                ink: template.colors.ink,
                captions: {enabled: true, mode: template.captionMode},
                defaultTransition: template.defaultTransition,
                transitionDurationS: template.transitionDurationS,
              },
            }}
            calculateMetadata={calculateMetadata}
          />
        ))}
      </Folder>
    </>
  );
};

registerRoot(RemotionRoot);
