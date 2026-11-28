import { requestAttentionLabel } from "../requestState";
import type { DownloadTray } from "../slices/requests";
import { useStore } from "../store";
import { RoomIcon } from "./RoomIcon";
import { RoomAction } from "./RoomControls";

type Props = { weeklyRows?: DownloadTray["rows"]; toolbar?: boolean };

export function RequestAttentionButton({ weeklyRows, toolbar = false }: Props) {
  const store = useStore();
  const rows = weeklyRows ?? store.requestsSlice.attentionRows;
  const open = () => store.requestsSlice.openDownloadTray({ source: weeklyRows ? "weekly" : "requests", rows });
  if (toolbar) return <button type="button" className="announcements-button"
    aria-label={rows.length ? `Needs review, ${rows.length} ${rows.length === 1 ? "request" : "requests"}` : "Needs review"}
    title="Needs review" aria-haspopup="dialog" aria-expanded={store.requestsSlice.downloadTray?.source === "requests"}
    onClick={open}>
    <RoomIcon name="alert" />
    {rows.length > 0 && <span className="announcements-badge" aria-hidden="true">{rows.length > 9 ? "9+" : rows.length}</span>}
  </button>;
  if (!rows.length) return null;
  return <RoomAction icon="chevronRight" aria-haspopup="dialog"
    onClick={open}>
    {requestAttentionLabel(rows.length)}
  </RoomAction>;
}
