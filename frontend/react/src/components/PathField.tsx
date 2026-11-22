import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@ui-sdk/react/button";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogDescription,
  DialogTitle,
} from "@ui-sdk/react/dialog";
import { Field, FieldLabel } from "@ui-sdk/react/field";
import { Input } from "@ui-sdk/react/input";
import { api, type BrowseResult } from "../api";

// A path field (item 36): the text input plus a Browse button that
// opens an in-app directory browser backed by the host-only
// /api/browse endpoint. A server-side path cannot be picked from the
// client machine's file dialog, so the browser runs in the app; raw
// path typing stays possible.
export function PathField({
  name,
  label,
  value,
  onValueChange,
  mode = "folder",
  required = false,
  disabled = false,
  placeholder,
  autoComplete,
}: {
  name: string;
  label: string;
  value: string;
  onValueChange: (value: string) => void;
  mode?: "folder" | "file";
  required?: boolean;
  disabled?: boolean;
  placeholder?: string;
  autoComplete?: string;
}) {
  const [open, setOpen] = useState(false);
  const [browse, setBrowse] = useState<BrowseResult | null>(null);
  const [browseError, setBrowseError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (path: string) => {
      setLoading(true);
      setBrowseError(null);
      try {
        const params = new URLSearchParams({ mode });
        if (path) params.set("path", path);
        setBrowse(await api<BrowseResult>(`/api/browse?${params}`));
      } catch (error) {
        setBrowseError(
          error instanceof Error ? error.message : "That folder could not be opened",
        );
      } finally {
        setLoading(false);
      }
    },
    [mode],
  );

  useEffect(() => {
    if (!open) return;
    // Start where the field points: the folder itself in folder
    // mode, its parent folder in file mode; fall back to the home
    // folder when that path cannot be browsed.
    const start =
      mode === "file" && value
        ? value.replace(/\/[^/]*$/, "") || "/"
        : value;
    void (async () => {
      setLoading(true);
      setBrowseError(null);
      try {
        const params = new URLSearchParams({ mode });
        if (start) params.set("path", start);
        setBrowse(await api<BrowseResult>(`/api/browse?${params}`));
      } catch {
        try {
          setBrowse(
            await api<BrowseResult>(`/api/browse?${new URLSearchParams({ mode })}`),
          );
        } catch (error) {
          setBrowseError(
            error instanceof Error
              ? error.message
              : "That folder could not be opened",
          );
        }
      } finally {
        setLoading(false);
      }
    })();
  }, [open, mode, value]);

  const choose = (path: string) => {
    onValueChange(path);
    setOpen(false);
  };

  return (
    <Field name={name}>
      <FieldLabel>{label}</FieldLabel>
      <div className="path-field">
        <Input
          value={value}
          onValueChange={onValueChange}
          required={required}
          disabled={disabled}
          placeholder={placeholder}
          autoComplete={autoComplete}
          spellCheck={false}
        />
        <Button
          className="quiet-button"
          type="button"
          disabled={disabled}
          onClick={() => setOpen(true)}
        >
          Browse
        </Button>
      </div>
      <Dialog open={open} onOpenChange={(next: boolean) => setOpen(next)}>
        <DialogBackdrop />
        <RoomDialogContent className="browse-dialog">
          <DialogClose
            className="dialog-close"
            aria-label="Close folder browser"
          >
            <RoomIcon name="close" />
          </DialogClose>
          <DialogTitle>
            {mode === "file" ? "Choose a file" : "Choose a folder"}
          </DialogTitle>
          <DialogDescription className="browse-path">
            {browse ? browse.path : "Reading the server computer…"}
          </DialogDescription>
          <div className="browse-nav">
            <Button
              className="quiet-button"
              type="button"
              onClick={() => void load("")}
            >
              Home
            </Button>
            <Button
              className="quiet-button"
              type="button"
              disabled={!browse?.parent}
              onClick={() => browse && void load(browse.parent)}
            >
              Up one level
            </Button>
          </div>
          <div className="browse-list">
            {browseError ? (
              <p className="browse-error" role="alert">
                {browseError}{" "}
                <button
                  className="quiet-button"
                  type="button"
                  onClick={() => void load(browse?.path || "")}
                >
                  Try again
                </button>
              </p>
            ) : loading && !browse ? (
              <p className="settings-note" role="status">
                Reading folder…
              </p>
            ) : browse && browse.entries.length ? (
              browse.entries.map((entry) =>
                entry.kind === "folder" ? (
                  <button
                    className="browse-item"
                    type="button"
                    key={entry.path}
                    onClick={() => void load(entry.path)}
                  >
                    <span aria-hidden="true"><RoomIcon name="chevronRight" /></span>
                    {entry.name}
                  </button>
                ) : (
                  <button
                    className="browse-item"
                    type="button"
                    key={entry.path}
                    onClick={() => choose(entry.path)}
                  >
                    <span aria-hidden="true">·</span>
                    {entry.name}
                  </button>
                ),
              )
            ) : (
              <p className="settings-note">
                {mode === "file"
                  ? "No folders or files here."
                  : "No folders here."}
              </p>
            )}
          </div>
          {mode === "folder" ? (
            <div className="settings-actions">
              <Button
                className="primary-action"
                type="button"
                disabled={!browse}
                onClick={() => browse && choose(browse.path)}
              >
                Use this folder
              </Button>
              <Button
                className="quiet-button"
                type="button"
                onClick={() => setOpen(false)}
              >
                Cancel
              </Button>
            </div>
          ) : null}
        </RoomDialogContent>
      </Dialog>
    </Field>
  );
}
