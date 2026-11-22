import { api, type Announcement } from "../api";
import { notify } from "../toasts";
import { deviceId } from "../store";
import type { SliceHost } from "./host";

// Announcements (plan 056): the household message list, the
// per-device last-seen marker, and the dialog's open state.
export class AnnouncementsSlice {
  announcements: Announcement[] = [];
  announcementsReady = false;
  lastSeenAnnouncement =
    Number(localStorage.getItem(`listening-announcement-seen-${deviceId}`)) || 0;
  announcementsOpen = false;

  constructor(private readonly host: SliceHost) {}

  get unreadAnnouncements(): number {
    return this.announcements.filter((item) => item.id > this.lastSeenAnnouncement)
      .length;
  }

  async pollAnnouncements() {
    try {
      const result = await api<{ announcements: Announcement[] }>(
        `/api/announcements?device_id=${encodeURIComponent(deviceId)}`,
      );
      const previousLatest = this.announcements.at(-1)?.id || 0;
      this.announcements = result.announcements;
      const latest = this.announcements.at(-1)?.id || 0;
      if (latest && latest < this.lastSeenAnnouncement) {
        this.lastSeenAnnouncement = 0;
        localStorage.removeItem(`listening-announcement-seen-${deviceId}`);
      }
      const fresh = this.announcements.filter(
        (item) =>
          item.id >
          Math.max(
            latest < previousLatest ? 0 : previousLatest,
            this.lastSeenAnnouncement,
          ),
      );
      if (this.announcementsReady && fresh.length && !this.announcementsOpen) {
        const item = fresh.at(-1)!;
        notify(
          `${item.sender}: ${item.message}${fresh.length > 1 ? ` · ${fresh.length} new messages` : ""}`,
        );
      }
      this.announcementsReady = true;
      if (this.announcementsOpen && latest) {
        this.lastSeenAnnouncement = latest;
        localStorage.setItem(
          `listening-announcement-seen-${deviceId}`,
          String(latest),
        );
      }
      this.host.bump();
    } catch {
      // The announcements dialog (Stage 4) renders its own error state.
    }
  }

  setAnnouncementsOpen(open: boolean) {
    this.announcementsOpen = open;
    if (open) {
      const latest = this.announcements.at(-1)?.id || 0;
      if (latest) {
        this.lastSeenAnnouncement = latest;
        localStorage.setItem(
          `listening-announcement-seen-${deviceId}`,
          String(latest),
        );
      }
    }
    this.host.bump();
  }
}
