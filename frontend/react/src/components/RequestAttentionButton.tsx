import { requestAttentionLabel, requestNeedsAttention, requestStage } from "../requestState";
import type { DownloadTray } from "../slices/requests";
import { useStore } from "../store";
import { RoomAction } from "./RoomControls";

type Props = { weeklyRows?: DownloadTray["rows"] };

export function RequestAttentionButton({ weeklyRows }: Props) {
  const store = useStore();
  const rows = weeklyRows ?? store.requestRows.filter(row => requestNeedsAttention(requestStage(row)));
  if (!rows.length) return null;
  return <RoomAction icon="chevronRight" aria-haspopup="dialog"
    onClick={() => store.requestsSlice.openDownloadTray({ source: weeklyRows ? "weekly" : "requests", rows })}>
    {requestAttentionLabel(rows.length)}
  </RoomAction>;
}
