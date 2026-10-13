import { api } from "../api/client";
import { isMediaArtifact } from "../lib/media";
import { useApi } from "../lib/useApi";
import { MediaPreview } from "./Artifacts";
import { AsyncBlock, Empty } from "./ui";

/** Image/video artifacts of one job, previewed through authenticated blob URLs. */
export function MediaGallery({ jobId, version }: { jobId: string; version?: number }) {
  const arts = useApi(`artifacts:${jobId}`, (s) => api.artifacts(jobId, s), { version });
  return (
    <AsyncBlock result={arts}>
      {(list) => {
        const media = list.filter(isMediaArtifact);
        if (!media.length) return <Empty>Keine Bilder oder Videos für diesen Job.</Empty>;
        return (
          <div className="media-grid" data-testid="media-grid">
            {media.map((a) => (
              <MediaPreview key={a.id} artifact={a} />
            ))}
          </div>
        );
      }}
    </AsyncBlock>
  );
}
