import { RoomIcon } from "./RoomIcon";
import { Toggle } from "@ui-sdk/react/toggle";
import { useStore } from "../store";

// The favorite control (plan 048): a ui-sdk Toggle carrying the
// room's favorite state for one catalog track. The store owns the
// state and the optimistic flip; this only renders the heart and
// asks for the toggle. Pressed reads accent, unpressed reads
// quiet, from the existing tokens (styles.css).
export function FavoriteButton({
  track,
  className = "",
}: {
  track: { id: number; title: string; favorite: boolean };
  className?: string;
}) {
  const store = useStore();
  const favorite = !!track.favorite;
  return (
    <Toggle
      className={`favorite-button${className ? ` ${className}` : ""}`}
      pressed={favorite}
      onPressedChange={() => void store.toggleFavorite(track)}
      aria-label={
        favorite
          ? `Remove ${track.title} from favorites`
          : `Favorite ${track.title}`
      }
      title={favorite ? "Remove from favorites" : "Favorite"}
    >
      <RoomIcon name="favorites" />
    </Toggle>
  );
}
