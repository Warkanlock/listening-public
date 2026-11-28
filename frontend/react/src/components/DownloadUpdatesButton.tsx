import type { RequestRow } from "../api";
import { requestAttentionLabel, requestNeedsAttention, requestStage } from "../requestState";
import type { DownloadTray } from "../slices/requests";
import { useStore } from "../store";
import { RoomIcon } from "./RoomIcon";
import { RoomAction } from "./RoomControls";

type Props = { weeklyRows?: readonly RequestRow[]; toolbar?: boolean };

export function DownloadUpdatesButton({ weeklyRows, toolbar = false }: Props) {
  const store = useStore();
  const tray: DownloadTray = weeklyRows ? { kind: "weekly", rows: weeklyRows } : { kind: "room" };
  const count = store.requestsSlice.updateRows(tray).filter(row => requestNeedsAttention(requestStage(row))).length;
  const open = () => store.requestsSlice.openDownloadTray(tray);
  if (toolbar) return <button type="button" className="announcements-button"
    aria-label={count ? `Download updates, ${requestAttentionLabel(count)}` : "Download updates"}
    title="Download updates" aria-haspopup="dialog" aria-expanded={store.requestsSlice.downloadTray !== null}
    onClick={open}>
    <RoomIcon name="inbox" />
    {count > 0 && <span className="announcements-badge" aria-hidden="true">{count > 9 ? "9+" : count}</span>}
  </button>;
  if (!count) return null;
  return <RoomAction icon="inbox" aria-haspopup="dialog" onClick={open}>
    {requestAttentionLabel(count)}
  </RoomAction>;
}
