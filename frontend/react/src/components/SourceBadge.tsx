import type { PlaylistSummary } from "../api";
import { RoomIcon } from "./RoomIcon";

type PlaylistSource = Pick<PlaylistSummary, "source" | "spotify_link">;

export function playlistSourceLabel(playlist: PlaylistSource) {
  switch (playlist.spotify_link?.mode) {
    case "two_way": return "Spotify · two-way sync";
    case "read_only": return "Spotify · read-only";
    case "paused": return "Spotify · sync paused";
    case "disconnected": return "Spotify · disconnected";
    case "multiple": return "Spotify · multiple links";
  }
  switch (playlist.source) {
    case "spotify": return "Spotify · linked";
    case "spotify-paused": return "Spotify · sync paused";
    case "linked": return "Linked source";
    case "watched": return "Watched folder";
    default: return "";
  }
}

export function SourceBadge({ playlist }: { playlist: PlaylistSource }) {
  const label = playlistSourceLabel(playlist);
  if (!label) return null;
  return <span className="source-badge" title={label} aria-label={label}>
    <RoomIcon name="magnet" />{label}
  </span>;
}
