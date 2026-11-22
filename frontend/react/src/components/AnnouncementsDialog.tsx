import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogTitle,
} from "@ui-sdk/react/dialog";
import { useStore } from "../store";
import { StateCard } from "./StateCard";

// The announcements dialog (parity item 10): the poll, unread badge,
// and seen-tracking live in the store; this is the listing half,
// newest first, on the ui-sdk Dialog.
export function AnnouncementsDialog() {
  const store = useStore();
  const items = [...store.announcements].reverse();
  return (
    <Dialog
      open={store.announcementsOpen}
      onOpenChange={(open: boolean) => store.setAnnouncementsOpen(open)}
    >
      <DialogBackdrop />
      <RoomDialogContent className="announcements-dialog">
        <div className="announcement-dialog-head">
          <div>
            <p className="hero-eyebrow">Listen together</p>
            <DialogTitle>Announcements</DialogTitle>
          </div>
          <DialogClose className="dialog-close" aria-label="Close announcements">
            <RoomIcon name="close" />
          </DialogClose>
        </div>
        <div className="announcement-list">
          {items.length ? (
            items.map((item) => (
              <article className="announcement-item" key={item.id}>
                <div>
                  <strong>{item.sender}</strong>
                  <time
                    dateTime={new Date(item.sent_at * 1000).toISOString()}
                  >
                    {new Date(item.sent_at * 1000).toLocaleString()}
                  </time>
                </div>
                <p>{item.message}</p>
              </article>
            ))
          ) : store.announcementsReady ? (
            <StateCard
              icon="announcements"
              label="Shared room"
              title="No announcements yet"
              body="Messages from paired browsers will appear here."
              compact
            />
          ) : (
            <StateCard
              kind="loading"
              icon="announcements"
              label="Shared room"
              title="Checking room messages"
              body="Looking for messages from paired browsers."
              compact
            />
          )}
        </div>
      </RoomDialogContent>
    </Dialog>
  );
}
