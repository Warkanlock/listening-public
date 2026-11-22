const entityKinds = ["artist", "recording", "release-group"] as const;
type EntityKind = typeof entityKinds[number];

export type ConnectionNode = {
  kind: EntityKind;
  id: string;
  title: string;
  detail: string;
};
export type ConnectionEdge = { node: ConnectionNode; label: string; note: string };
export type TrackCredit = { title: string; artist: string; album: string };
export type ConnectionGraph = (
  | { state: "ready"; root: ConnectionNode; edges: ConnectionEdge[] }
  | { state: "unmatched"; title: string; artist: string; credit: ConnectionNode | null; retry: boolean }) & { library?: TrackCredit };
export type SongInfoMode = "details" | "song" | "album" | "artist-profile" | "album-tracks";
export type ConnectionTarget = Pick<ConnectionNode, "kind" | "id"> | { kind: "song" | "album"; id: number };

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function entityKind(value: unknown): value is EntityKind {
  return entityKinds.some(kind => kind === value);
}

function connectionNode(value: unknown): ConnectionNode | null {
  if (!object(value) || !entityKind(value.kind) || typeof value.id !== "string" ||
    !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value.id) ||
    typeof value.title !== "string" || typeof value.detail !== "string") return null;
  return { kind: value.kind, id: value.id, title: value.title, detail: value.detail };
}

export function parseConnectionGraph(value: unknown): ConnectionGraph {
  if (object(value)) {
    const record = value.library;
    const library = object(record) && typeof record.title === "string" && typeof record.artist === "string" &&
      typeof record.album === "string" ? { title: record.title, artist: record.artist, album: record.album } : undefined;
    if (value.state === "unmatched" && typeof value.title === "string" && typeof value.artist === "string") {
      return { state: "unmatched", title: value.title, artist: value.artist, credit: connectionNode(value.credit), library, retry: value.retry === true };
    }
    if (value.state === "ready" && Array.isArray(value.edges)) {
      const root = connectionNode(value.root);
      if (root) {
        const edges: ConnectionEdge[] = [];
        for (const edge of value.edges.slice(0, 9)) {
          if (!object(edge) || typeof edge.label !== "string" || typeof edge.note !== "string") continue;
          const node = connectionNode(edge.node);
          if (node) edges.push({ node, label: edge.label, note: edge.note });
        }
        return { state: "ready", root, edges, library };
      }
    }
  }
  throw new Error("Artist credits could not be read.");
}

export function canOpenMusicProfile(id: string | number) {
  return typeof id === "number" || /^custom-[1-9][0-9]*$/.test(id);
}
