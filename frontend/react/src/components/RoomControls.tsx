import { RoomAlertContent } from "./WindowContent";
import { useId, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { Button } from "@ui-sdk/react/button";
import { AlertDialog, AlertDialogBackdrop, AlertDialogClose, AlertDialogDescription, AlertDialogTitle } from "@ui-sdk/react/alert-dialog";
import { Select, SelectContent, SelectItem, SelectItemIndicator, SelectLabel, SelectTrigger, SelectValue } from "@ui-sdk/react/select";
import { RoomIcon, type RoomIconName } from "./RoomIcon";

type ActionProps = Omit<ComponentPropsWithoutRef<typeof Button>, "children"> & { children: ReactNode; icon?: RoomIconName; primary?: boolean };

export function RoomAction({ icon, primary, children, type = "button", ...props }: ActionProps) {
  const appearance = primary ? "room-action primary-action" : "room-action quiet-button";
  return <Button type={type} className={appearance} {...props}>
    {icon && <RoomIcon name={icon} />}{children}
  </Button>;
}

export function RoomConfirm({ open, title, description, confirmLabel, onClose, onConfirm }: {
  open: boolean; title: string; description: string; confirmLabel: string; onClose: () => void; onConfirm: () => void;
}) {
  return <AlertDialog open={open} onOpenChange={(next, details) => {
    if (!next) {
      onClose();
      if (details.returnValue === "confirm") onConfirm();
    }
  }}>
    <AlertDialogBackdrop />
    <RoomAlertContent className="import-conflict-dialog">
      <AlertDialogTitle>{title}</AlertDialogTitle>
      <AlertDialogDescription>{description}</AlertDialogDescription>
      <div className="settings-actions import-conflict-actions">
        <AlertDialogClose className="quiet-button room-action" value="cancel">Cancel</AlertDialogClose>
        <AlertDialogClose className="primary-action room-action" value="confirm">{confirmLabel}</AlertDialogClose>
      </div>
    </RoomAlertContent>
  </AlertDialog>;
}

type Choice = { value: string; label: string; disabled?: boolean; title?: string; icon?: RoomIconName };

export const audioExportFormats = [
  { value: "original", label: "Original files" }, { value: "opus", label: "Compact · Opus" }, { value: "aac", label: "Compact · AAC" },
];

export function RoomSelect({ label, value, options, onChange, disabled, hideLabel, describedBy, description }: {
  label: string; value: string; options: Choice[]; onChange: (value: string) => void;
  disabled?: boolean; hideLabel?: boolean; describedBy?: string; description?: string;
}) {
  const descriptionId = useId();
  return <div className="room-select">
    <Select value={value} items={options} disabled={disabled} onValueChange={next => { if (next != null) onChange(next); }}>
      {!hideLabel && <div className="room-select-caption">
        <SelectLabel>{label}</SelectLabel>
        {description && <p id={descriptionId} className="settings-note">{description}</p>}
      </div>}
      <SelectTrigger aria-label={label} aria-describedby={description && !hideLabel ? descriptionId : describedBy}>
        <SelectValue /><RoomIcon name="chevronDown" />
      </SelectTrigger>
      <SelectContent className="room-select-menu">
        {options.map(choice => <SelectItem key={choice.value} value={choice.value} data-value={choice.value} disabled={choice.disabled} title={choice.title} label={choice.label}>
          {choice.label}{choice.icon && <RoomIcon name={choice.icon} />}<SelectItemIndicator><RoomIcon name="check" /></SelectItemIndicator>
        </SelectItem>)}
      </SelectContent>
    </Select>
  </div>;
}
