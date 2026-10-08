import type { Artifact, HermEvent } from "../api/types";

export function isMediaArtifact(a: Pick<Artifact, "media_type" | "kind">): boolean {
  return a.media_type.startsWith("image/") || a.media_type.startsWith("video/") || a.kind === "image" || a.kind === "video";
}

export function isMediaEvent(ev: HermEvent): boolean {
  return ev.event_type.startsWith("media.");
}
