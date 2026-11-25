import { useEffect, useState } from "react";
import { RoomIconButton } from "./RoomIcon";

export function RefreshButton({ target, onRefresh }: {
  target: "requests" | "jobs";
  onRefresh: () => Promise<boolean>;
}) {
  const [state, setState] = useState<"idle" | "refreshing" | "done" | "failed">("idle");
  const busy = state === "refreshing";
  const label = `Refresh ${target}`;
  const message = {
    idle: "",
    refreshing: `Refreshing ${target}…`,
    done: `Refreshed ${target}`,
    failed: `Couldn’t refresh ${target}. Try again.`,
  }[state];

  useEffect(() => {
    if (state !== "done") return;
    const timeout = window.setTimeout(() => setState("idle"), 1800);
    return () => window.clearTimeout(timeout);
  }, [state]);

  async function refresh() {
    if (busy) return;
    setState("refreshing");
    try {
      setState(await onRefresh() ? "done" : "failed");
    } catch {
      setState("failed");
    }
  }

  return <>
    <RoomIconButton icon={state === "done" ? "check" : state === "failed" ? "alert" : "refresh"}
      label={label} title={message || label} data-refresh-state={state}
      aria-busy={busy} aria-disabled={busy} onClick={() => void refresh()} />
    <span className="visually-hidden" role="status">{message}</span>
  </>;
}
