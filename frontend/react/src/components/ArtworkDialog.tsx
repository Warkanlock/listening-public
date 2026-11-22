import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogDescription,
  DialogTitle,
} from "@ui-sdk/react/dialog";
import { useStore } from "../store";
import { ArtworkStage, useArtworkView } from "./Artwork";

// The artwork dialog (amended item 34): the stage cycles the four
// artwork views on click, starting from the remembered view and
// remembering each change; there is no toggle button.
export function ArtworkDialog() {
  const store = useStore();
  const { view, cycle } = useArtworkView();
  const subject = store.artworkSubject;
  if (!subject) return null;

  const cycleFromKeyboard = (event: React.KeyboardEvent) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    cycle();
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) store.closeArtwork();
      }}
    >
      <DialogBackdrop />
      <RoomDialogContent className="artwork-dialog">
        <DialogClose className="dialog-close" aria-label="Close artwork">
          <RoomIcon name="close" />
        </DialogClose>
        <p className="hero-eyebrow">Now playing artwork</p>
        <DialogTitle>{subject.title}</DialogTitle>
        <DialogDescription>{subject.artist}</DialogDescription>
        <div
          className="artwork-stage"
          role="button"
          tabIndex={0}
          aria-label="Change artwork view"
          title="Change artwork view"
          onClick={cycle}
          onKeyDown={cycleFromKeyboard}
        >
          <ArtworkStage subject={subject} view={view} />
        </div>
        <p className="artwork-view-label">
          {view === "case" ? "CD case" : `${view} artwork`}
        </p>
      </RoomDialogContent>
    </Dialog>
  );
}
