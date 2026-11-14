/**
 * YouTube · Editorial — the house style for 16:9 long-form.
 * White paper, thin ink rules, serif lower thirds, mono scene index.
 */

import type React from "react";
import {AbsoluteFill, interpolate, useCurrentFrame} from "remotion";

import {EditorialCard} from "../cards";
import {MONO, entrance} from "../motion";
import {Backdrop, SceneFrame} from "../scene";
import type {CardProps, SceneRendererProps, TemplateComponent} from "../template";

const COLORS = {accent: "#D6FF45", background: "#FFFFFF", ink: "#111111"};

const Scene: React.FC<SceneRendererProps> = ({spec, scene, frames}) => {
  const frame = useCurrentFrame();
  const index = interpolate(frame, [0, 16], [0, 1], {extrapolateRight: "clamp"});
  return (
    <SceneFrame spec={spec} scene={scene} frames={frames} captionStyle="lower-third">
      <Backdrop spec={spec} scene={scene} frame={frame} frames={frames} />
      {/* A code scene is its own design: the paper border and mono index would sit on top of the
          author's composition. The template keeps only this template's framing, not its typography. */}
      {scene.mode !== "code" && (
        <>
          <AbsoluteFill style={{border: "2px solid #111111", margin: "1.4%"}} />
          <div
            style={{
              position: "absolute",
              left: "3.6%",
              top: "3.8%",
              display: "flex",
              gap: 12,
              alignItems: "baseline",
              fontFamily: MONO,
              fontSize: "1.05cqw",
              letterSpacing: "0.18em",
              textTransform: "uppercase",
              color: "#111111",
              opacity: index,
              transform: entrance("slide", index).transform,
            }}
          >
            {String(scene.index).padStart(2, "0")} / {String(spec.scenes.length).padStart(2, "0")}
            {scene.title ? <span style={{marginLeft: 12, opacity: 0.55}}>{scene.title}</span> : null}
          </div>
        </>
      )}
    </SceneFrame>
  );
};

const Title: React.FC<CardProps> = (props) => (
  <EditorialCard {...props} {...COLORS} inverted />
);

const Outro: React.FC<CardProps> = (props) => <EditorialCard {...props} {...COLORS} />;

export const YouTubeEditorial: TemplateComponent = {
  id: "youtube-editorial",
  name: "YouTube · Editorial",
  description: "White paper, ink rules, serif lower thirds — the house style for long-form.",
  width: 1920,
  height: 1080,
  fps: 30,
  colors: COLORS,
  captionMode: "lower-third",
  defaultTransition: "fade",
  transitionDurationS: 0.6,
  Title,
  Outro,
  Scene,
};
