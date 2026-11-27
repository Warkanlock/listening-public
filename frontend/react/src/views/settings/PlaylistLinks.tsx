import { useEffect, useState } from "react";
import { api, postJson, readable } from "../../api";
import { RoomAction, RoomSelect } from "../../components/RoomControls";
import { RoomIcon } from "../../components/RoomIcon";
import { useStore } from "../../store";
import { errorText } from "./shared";

interface Candidate {
  base: string;
  source: string;
  fixed_base: boolean;
  matched: number;
  matching: string[];
  base_only: string[];
  source_only: string[];
  ambiguous: string[];
}
interface Review {
  sources: Record<string, string>;
  links: { id: string; name: string; sources: string[] }[];
  candidates: Candidate[];
  dismissed: [string, string][];
}
type MatchOperation =
  | { op: "confirm"; base: string; source: string; name: string }
  | { op: "group" | "dismiss" | "reconsider"; base: string; source: string }
  | { op: "unlink"; id: string };
type Action = (operation: MatchOperation) => Promise<boolean>;

const ROWS_PER_PAGE = 6;
const SONGS_PER_PAGE = 30;

function sourceName(sources: Review["sources"], key: string) {
  return sources[key] || key;
}

function TrackDifferences({ label, tracks }: { label: string; tracks: string[] }) {
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState(SONGS_PER_PAGE);
  return (
    <details data-settings-list-disclosure="tracks" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>
        <span>{label}</span>
        <span data-settings-list-meta>{readable(tracks.length)}</span>
        <RoomIcon name="chevronDown" />
      </summary>
      {open && (
        <div data-settings-list-content>
          <ul data-playlist-match-songs>{tracks.slice(0, limit).map((track, index) => <li key={index}>{track}</li>)}</ul>
          {tracks.length > limit && (
            <RoomAction onClick={() => setLimit(limit + SONGS_PER_PAGE)}>Show more songs</RoomAction>
          )}
        </div>
      )}
    </details>
  );
}

function Match({ candidate, sources, act, busy }: {
  candidate: Candidate;
  sources: Review["sources"];
  act: Action;
  busy: boolean;
}) {
  const baseName = sourceName(sources, candidate.base);
  const extraName = sourceName(sources, candidate.source);
  const sameName = baseName === extraName;
  const [base, setBase] = useState(candidate.base);
  const [name, setName] = useState<string | null>(null);
  const extra = base === candidate.base ? candidate.source : candidate.base;
  const playlistName = name ?? sourceName(sources, base);
  const differences = [
    { key: "matching", label: "In both playlists", tracks: candidate.matching },
    { key: "base-only", label: `Only in ${sameName ? "the first playlist" : baseName}`, tracks: candidate.base_only },
    { key: "source-only", label: `Only in ${sameName ? "the second playlist" : extraName}`, tracks: candidate.source_only },
    { key: "ambiguous", label: "Recordings to check", tracks: candidate.ambiguous },
  ].filter(({ tracks }) => tracks.length > 0);

  return (
    <details data-settings-list-disclosure="match">
      <summary>
        <span data-playlist-match-pair>
          <strong>{baseName}</strong>
          <span>and <strong>{extraName}</strong></span>
        </span>
        <span data-settings-list-meta>
          {readable(candidate.matched)} {candidate.matched === 1 ? "recording" : "recordings"} in common
        </span>
        <RoomIcon name="chevronDown" />
      </summary>
      <div data-settings-list-content>
        {sameName && (
          <dl data-playlist-match-sources>
            <div><dt>First source</dt><dd>{candidate.base}</dd></div>
            <div><dt>Second source</dt><dd>{candidate.source}</dd></div>
          </dl>
        )}
        {differences.length > 0 && (
          <div data-playlist-match-differences>
            {differences.map(({ key, label, tracks }) => <TrackDifferences key={key} label={label} tracks={tracks} />)}
          </div>
        )}
        <div data-playlist-match-fields>
          <RoomSelect
            label="Keep the order of"
            value={base}
            disabled={busy || candidate.fixed_base}
            onChange={value => { setBase(value); setName(null); }}
            options={[candidate.base, candidate.source].map((value, index) => ({
              value,
              label: sameName ? `${sourceName(sources, value)} (${index === 0 ? "first" : "second"} playlist)` : sourceName(sources, value),
            }))}
          />
          {!candidate.fixed_base && (
            <label data-settings-list-field>
              Playlist name
              <input value={playlistName} maxLength={120} disabled={busy} onChange={event => setName(event.target.value)} />
            </label>
          )}
        </div>
        <div data-settings-list-actions>
          <RoomAction primary disabled={busy || !playlistName.trim()}
            onClick={() => act({ op: "confirm", base, source: extra, name: playlistName })}>Link playlists</RoomAction>
          <RoomAction disabled={busy}
            onClick={() => act({ op: "dismiss", base, source: extra })}>Keep separate</RoomAction>
        </div>
      </div>
    </details>
  );
}

export function PlaylistLinks({ revision }: { revision: string }) {
  const store = useStore();
  const [review, setReview] = useState<Review | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [base, setBase] = useState("");
  const [extra, setExtra] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [suggestionsShown, setSuggestionsShown] = useState(ROWS_PER_PAGE);
  const [linksShown, setLinksShown] = useState(ROWS_PER_PAGE);
  const [dismissedShown, setDismissedShown] = useState(ROWS_PER_PAGE);
  const busy = loading || saving;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void api<Review>("/api/playlist-links", { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) { setReview(data); setMessage(""); }
    }).catch(error => {
      if (!controller.signal.aborted) setMessage(errorText(error, "Could not load playlist matches"));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [revision, refresh]);

  const act: Action = async operation => {
    if (busy) return false;
    setSaving(true);
    try {
      setReview(await postJson<Review>("/api/playlist-links", operation));
      await store.refreshOverview();
      await store.loadTracks(true);
      setMessage("");
      return true;
    } catch (error) {
      setMessage(errorText(error, "Could not update playlist links"));
      return false;
    } finally {
      setSaving(false);
    }
  };

  const grouped = new Set(review?.links.flatMap(link => link.sources));
  const choices = Object.entries(review?.sources ?? {}).filter(([key]) => !grouped.has(key));
  const nameCounts = new Map<string, number>();
  for (const [, name] of choices) nameCounts.set(name, (nameCounts.get(name) ?? 0) + 1);
  const options = [
    { value: "", label: "Choose a playlist" },
    ...choices.map(([id, name]) => ({ value: id, label: (nameCounts.get(name) ?? 0) > 1 ? `${name} (${id})` : name })),
  ];
  const validBase = choices.some(([id]) => id === base);
  const validExtra = choices.some(([id]) => id === extra);

  return (
    <section className="settings-card" data-settings-lists="matches" aria-labelledby="playlist-matches-heading">
      <div data-settings-list-head>
        <div>
          <h3 id="playlist-matches-heading">Playlist matches</h3>
          <p>Link playlists from different sources so they appear together in Listening.</p>
        </div>
        <RoomAction icon="refresh" disabled={busy} onClick={() => setRefresh(n => n + 1)}>Refresh matches</RoomAction>
      </div>

      {message && <p data-settings-list-message role="alert">{message}</p>}
      {loading && !review && <p data-settings-list-message role="status">Looking for matching playlists…</p>}
      {review && (
        <>
          <section data-playlist-match-section aria-labelledby="suggested-matches-heading">
            <h4 id="suggested-matches-heading">Suggested matches</h4>
            {review.candidates.length > 0 ? (
              <div data-playlist-match-differences>
                {review.candidates.slice(0, suggestionsShown).map(candidate => (
                  <Match
                    key={JSON.stringify([candidate.base, candidate.source])}
                    candidate={candidate} sources={review.sources} act={act} busy={busy}
                  />
                ))}
              </div>
            ) : <p>No suggested matches.</p>}
            {review.candidates.length > suggestionsShown && (
              <RoomAction onClick={() => setSuggestionsShown(limit => limit + ROWS_PER_PAGE)}>
                Show more matches ({readable(review.candidates.length - suggestionsShown)})
              </RoomAction>
            )}
          </section>

          {choices.length > 1 && (
            <details data-settings-list-disclosure="manual">
              <summary><span>Group two playlists manually</span><RoomIcon name="chevronDown" /></summary>
              <form data-settings-list-content onSubmit={async event => {
                event.preventDefault();
                if (await act({ op: "group", base, source: extra })) {
                  setBase("");
                  setExtra("");
                }
              }}>
                <p>Pick the playlist whose name and order to keep in Listening.</p>
                <div data-playlist-match-fields>
                  <RoomSelect label="Keep the name and order of" value={validBase ? base : ""} onChange={setBase} disabled={busy}
                    options={options.map(option => ({ ...option, disabled: !!extra && option.value === extra }))} />
                  <RoomSelect label="Include songs from" value={validExtra ? extra : ""} onChange={setExtra} disabled={busy}
                    options={options.map(option => ({ ...option, disabled: !!base && option.value === base }))} />
                </div>
                <RoomAction type="submit" disabled={busy || !validBase || !validExtra}>Group in Listening</RoomAction>
              </form>
            </details>
          )}

          <section data-playlist-match-section aria-labelledby="linked-matches-heading">
            <h4 id="linked-matches-heading">Linked playlists</h4>
            {review.links.length > 0 ? (
              <ul data-settings-list-rows>
                {review.links.slice(0, linksShown).map(link => (
                  <li data-settings-list-row key={link.id}>
                    <div>
                      <strong>{link.name}</strong>
                      <span data-playlist-linked-sources>{link.sources.map(key => (
                        <span key={key} title={key}>{sourceName(review.sources, key)}</span>
                      ))}</span>
                    </div>
                    <RoomAction disabled={busy} onClick={() => act({ op: "unlink", id: link.id })}>Unlink</RoomAction>
                  </li>
                ))}
              </ul>
            ) : <p>No playlists linked through matches.</p>}
            {review.links.length > linksShown && (
              <RoomAction onClick={() => setLinksShown(limit => limit + ROWS_PER_PAGE)}>
                Show more linked playlists ({readable(review.links.length - linksShown)})
              </RoomAction>
            )}
          </section>

          {review.dismissed.length > 0 && (
            <details data-settings-list-disclosure="dismissed">
              <summary>
                <span>Kept separate</span>
                <span data-settings-list-meta>{readable(review.dismissed.length)}</span>
                <RoomIcon name="chevronDown" />
              </summary>
              <div data-settings-list-content>
                <ul data-settings-list-rows>
                  {review.dismissed.slice(0, dismissedShown).map(([base, source]) => (
                    <li data-settings-list-row key={JSON.stringify([base, source])}>
                      <div>
                        <strong>{sourceName(review.sources, base)}</strong>
                        <span>and {sourceName(review.sources, source)}</span>
                      </div>
                      <RoomAction disabled={busy} onClick={() => act({ op: "reconsider", base, source })}>Review again</RoomAction>
                    </li>
                  ))}
                </ul>
                {review.dismissed.length > dismissedShown && (
                  <RoomAction onClick={() => setDismissedShown(limit => limit + ROWS_PER_PAGE)}>
                    Show more kept separate ({readable(review.dismissed.length - dismissedShown)})
                  </RoomAction>
                )}
              </div>
            </details>
          )}
        </>
      )}
    </section>
  );
}
