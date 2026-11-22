import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import { RoomIcon, type RoomIconName } from "../../components/RoomIcon";

export function ExtensionIconAction({ label, icon, ...props }:
  Omit<ComponentPropsWithoutRef<typeof TooltipTrigger>, "children" | "className"> &
  { label: string; icon: RoomIconName }) {
  return <Tooltip type="label">
    <TooltipTrigger {...props} type="button" aria-label={label}
      className="quiet-button icon-control">
      <RoomIcon name={icon} />
    </TooltipTrigger>
    <TooltipContent className="extension-tooltip">{label}</TooltipContent>
  </Tooltip>;
}

export function retryTime(deadline?: number) {
  if (!deadline || deadline * 1000 <= Date.now()) return "";
  return new Date(Math.ceil(deadline / 60) * 60_000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function ExtensionInventory({ label, manage, children }: { label: string; manage?: boolean; children: ReactNode }) {
  return <div className="extension-inventory">
    <table className="extension-table" role="table" aria-label={label}>
      <thead role="rowgroup"><tr role="row">
        <th role="columnheader" scope="col">Extension</th>
        <th role="columnheader" scope="col">Version</th>
        <th role="columnheader" scope="col">Status</th>
        {manage && <th role="columnheader" scope="col" className="extension-action-column">Actions</th>}
      </tr></thead>
      {children}
    </table>
  </div>;
}

type InventoryEntry = { id: string; version: string; ready?: boolean; enabled?: boolean; retry_at?: number };

export function ExtensionInventoryRow({ engine, available, actions, settingsId, settingsOpen, children }: {
  engine: InventoryEntry; available?: boolean; actions?: ReactNode;
  settingsId?: string; settingsOpen?: boolean; children?: ReactNode;
}) {
  const retry = retryTime(engine.retry_at);
  const label = available ? "Not installed" : !engine.enabled ? "Disabled" :
    retry ? "Waiting to retry" : engine.ready ? "Enabled" : "Unavailable";
  return <tbody className="extension-table-group" role="rowgroup">
    <tr className="extension-table-row" role="row">
      <th scope="row" role="rowheader">{engine.id}</th>
      <td className="extension-version" role="cell">{engine.version}</td>
      <td className="extension-state" role="cell">
        <span className={`status-pill job-state${label === "Enabled" ? " job-state--running" :
          label === "Waiting to retry" ? " job-state--queued" : ""}`}>{label}</span>
      </td>
      {actions && <td className="extension-action-column" role="cell"><div className="extension-icon-actions">{actions}</div></td>}
    </tr>
    {(retry || children) && <tr className="extension-detail-row" role="row" hidden={!retry && !settingsOpen}>
      <td role="cell" colSpan={actions ? 4 : 3}>
        {retry && <p className="extension-notice" role="status">
          {engine.enabled ? `Sign-in failed. Listening can try again after ${retry}.` :
            `Sign-in failed. ${actions ? "You" : "The room owner"} can try enabling this extension after ${retry}.`}
        </p>}
        {children && <div id={settingsId} hidden={!settingsOpen}>{children}</div>}
      </td>
    </tr>}
  </tbody>;
}
