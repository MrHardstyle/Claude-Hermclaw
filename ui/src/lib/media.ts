import type { Artifact, HermEvent } from "../api/types";

export function isMediaArtifact(a: Pick<Artifact, "media_type" | "kind">): boolean {
  return a.media_type.startsWith("image/") || a.media_type.startsWith("video/") || a.kind === "image" || a.kind === "video";
}

export function isMediaEvent(ev: HermEvent): boolean {
  return ev.event_type.startsWith("media.");
}

/**
 * Re-type a downloaded artifact as an opaque attachment. Artifacts are produced by workers/models (untrusted);
 * a `blob:` URL inherits the UI's origin, so an HTML/SVG blob that the browser ever rendered instead of saving
 * could run script next to the session token. `application/octet-stream` can only be saved, never rendered.
 */
export function asAttachment(blob: Blob): Blob {
  return blob.type === "application/octet-stream" ? blob : new Blob([blob], { type: "application/octet-stream" });
}
