import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogTitle,
} from "@ui-sdk/react/dialog";
import { useStore } from "../store";

// The keyboard shortcuts dialog (parity item 9): the same shortcut
// list as the vanilla UI, on the ui-sdk Dialog.
export function ShortcutsDialog() {
  const store = useStore();
  return (
    <Dialog
      open={store.shortcutsOpen}
      onOpenChange={(open: boolean) => store.setShortcutsOpen(open)}
    >
      <DialogBackdrop />
      <RoomDialogContent className="shortcuts-dialog">
        <DialogClose className="dialog-close" aria-label="Close keyboard shortcuts">
          <RoomIcon name="close" />
        </DialogClose>
        <DialogTitle>Keyboard shortcuts</DialogTitle>
        <dl>
          <div>
            <dt>Space</dt>
            <dd>Play or pause</dd>
          </div>
          <div>
            <dt>⌘ K / Ctrl K</dt>
            <dd>Search music</dd>
          </div>
          <div>
            <dt>← / →</dt>
            <dd>Previous or next song</dd>
          </div>
          <div>
            <dt>Q</dt>
            <dd>Open the queue</dd>
          </div>
          <div>
            <dt>?</dt>
            <dd>Show shortcuts</dd>
          </div>
        </dl>
      </RoomDialogContent>
    </Dialog>
  );
}
