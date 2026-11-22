import type { RoomIconName } from "./components/RoomIcon";

// Destination presentation (plan 061 step 4): Favorites, Recently
// played and Most played are one domain concept (a destination)
// whose presentation used to be split three ways: navigation
// labels and icons in App.tsx, section title
// ternaries in Collection.tsx, and an empty-state cascade in
// TrackTable.tsx. The descriptor map below is the one home for
// that presentation; the surfaces consume it for the active
// destination. DESTINATION_FILTERS and isDestinationFilter keep
// their home in store.tsx, and the browsing slice keeps the
// filtering and loading semantics.
export type DestinationFilter = "favorites" | "recent" | "most";

export interface DestinationDescriptor {
  navLabel: string;
  headerTitle: string;
  emptyIcon: RoomIconName;
  emptyTitle: string;
  emptyBody: string;
}

export const DESTINATION_DESCRIPTORS: Record<
  DestinationFilter,
  DestinationDescriptor
> = {
  favorites: {
    navLabel: "Favorites",
    headerTitle: "Favorite songs",
    emptyIcon: "favorites",
    emptyTitle: "No favorites yet",
    emptyBody: "Tap the heart on any song to mark it as a favorite.",
  },
  recent: {
    navLabel: "Recently played",
    headerTitle: "Recently played",
    emptyIcon: "recent",
    emptyTitle: "Nothing played yet",
    emptyBody: "Songs the room plays will appear here, newest first.",
  },
  most: {
    navLabel: "Most played",
    headerTitle: "Most played",
    emptyIcon: "refresh",
    emptyTitle: "No completed plays yet",
    emptyBody: "When a song plays to the end, it starts counting here.",
  },
};
