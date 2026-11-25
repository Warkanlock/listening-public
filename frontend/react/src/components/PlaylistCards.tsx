import { readable, type PlaylistSummary } from "../api";
import { useStore } from "../store";
import { CoverArt } from "./Artwork";
import { SourceBadge } from "./SourceBadge";

export function PlaylistCards({ playlists }: { playlists: PlaylistSummary[] }) {
  const store = useStore();
  return <div className="playlist-cards">
    {playlists.map(playlist => <button className="playlist-card" type="button" key={playlist.id} onClick={() => {
      store.clearSearch();
      store.setView("all", playlist.id);
    }}>
      <span className="playlist-card-art" aria-hidden="true">
        <CoverArt cover={playlist.cover} fallback={playlist.name.trim().slice(0, 1)} />
      </span>
      <strong title={playlist.name}>{playlist.name}</strong>
      <span>
        {readable(playlist.playable)} ready · {readable(playlist.entries)} {playlist.entries === 1 ? "song" : "songs"}
        <SourceBadge playlist={playlist} />
      </span>
    </button>)}
  </div>;
}
