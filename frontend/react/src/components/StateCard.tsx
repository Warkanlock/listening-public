import { RoomIcon, type RoomIconName } from "./RoomIcon";
import type { ReactNode } from "react";

// The state-card pattern (parity item 33): one empty, loading, and
// error presentation for list surfaces, with a compact variant for
// inline follow-on errors such as a failed page load.
export function StateCard({
  kind = "empty",
  icon = "music",
  label,
  title,
  body,
  action,
  compact = false,
}: {
  kind?: "empty" | "loading" | "error";
  icon?: RoomIconName;
  label?: string;
  title: string;
  body: string;
  action?: ReactNode;
  compact?: boolean;
}) {
  const role =
    kind === "loading" ? "status" : kind === "error" ? "alert" : undefined;
  return (
    <div
      className={`ui-state ui-state--${kind}${compact ? " ui-state--compact" : ""}`}
      role={role}
      aria-live={kind === "loading" ? "polite" : undefined}
    >
      <span className="ui-state-icon" aria-hidden="true">
        <RoomIcon name={icon} />
      </span>
      <div className="ui-state-copy">
        {label ? <span className="ui-state-label">{label}</span> : null}
        <strong>{title}</strong>
        <p>{body}</p>
      </div>
      {action ? <div className="ui-state-actions">{action}</div> : null}
    </div>
  );
}

export function Spinner() {
  return <span className="room-spinner" aria-hidden="true" />;
}

export function LoadingStatus({ children }: { children: ReactNode }) {
  return <div className="room-loading-status" role="status"><Spinner />{children}</div>;
}

export function SkeletonLines({ count = 3 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, index) => (
        <span
          key={index}
          className={`ui-skeleton-line ui-skeleton-line--${index % 3}`}
          aria-hidden="true"
        />
      ))}
    </>
  );
}

// The track-list loading state: a caption plus four skeleton rows.
export function TrackLoading() {
  return (
    <div className="loading-tracks">
      <p className="loading-caption" role="status">
        Loading songs in this view
      </p>
      {Array.from({ length: 4 }, (_, index) => (
        <div className="track-skeleton" key={index} aria-hidden="true">
          <span className="skeleton-art" />
          <span className="skeleton-copy">
            <SkeletonLines count={2} />
          </span>
          <span className="skeleton-pill" />
        </div>
      ))}
    </div>
  );
}
