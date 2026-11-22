import type { RequestRow, RequestSong } from "../api";
import type { View } from "../store";

// The slices' shared contract (plan 056): the operations a slice
// may ask of the rest of the store. The Store implements this
// interface and hands itself to each slice at construction. A
// call that in the monolith reached another cluster goes through
// here, never across to a sibling slice directly.
export interface SliceHost {
  bump(): void;
  readonly view: View;
  readonly deviceName: string;
  readonly playbackPresenceActive: boolean;
  readonly playbackOutputHeaders: Record<string, string>;
  readonly parkedSongAt: number;
  sendSession(
    op: string,
    fields?: Record<string, unknown>,
    successMessage?: string,
  ): Promise<boolean>;
  createSongRequest(song: RequestSong): Promise<RequestRow | null>;
  loadRequests(): Promise<void>;
  loadDiscover(): Promise<void>;
  loadSettings(): Promise<void>;
  loadTracks(reset?: boolean): Promise<void>;
  refreshJobs(): Promise<void>;
  refreshOverview(): Promise<void>;
  refreshActivity(): Promise<void>;
  loadStations(): Promise<void>;
  stopRadio(clearShared?: boolean): void;
  pollSession(): Promise<void>;
  setActivityDialogOpen(open: boolean): void;
  resetRequestsSignature(): void;
  setSessionFavorite(id: number, favorite: boolean): void;
}

export const errorMessage = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;
