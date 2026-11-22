import { useLayoutEffect, useRef, type KeyboardEvent, type ReactNode, type Ref } from "react";
import { DialogContent } from "@ui-sdk/react/dialog";
import { AlertDialogContent } from "@ui-sdk/react/alert-dialog";

type Point = { x: number; y: number };
type WindowProps = { children: ReactNode; className: string };

const handles = ".window-grip, header, [data-slot='dialog-title'], [data-slot='alert-dialog-title'], .lyrics-stage-head";
const controls = "button, a, input, select, textarea, summary, [contenteditable=true]";

function place(element: HTMLElement, x: number, y: number) {
  element.dataset.floating = "true";
  const { width, height } = element.getBoundingClientRect();
  const bound = (value: number, space: number) => Math.min(Math.max(0, space), Math.max(0, value));
  element.style.setProperty("--window-x", bound(x, window.innerWidth - width) + "px");
  element.style.setProperty("--window-y", bound(y, window.innerHeight - height) + "px");
}

function center(element: HTMLElement) {
  delete element.dataset.floating;
  element.style.removeProperty("--window-x");
  element.style.removeProperty("--window-y");
}

function WindowGrip() {
  const grip = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; start: Point; offset: Point } | null>(null);

  useLayoutEffect(() => {
    const element = grip.current?.parentElement;
    if (!element) return;
    const keepVisible = () => {
      if (!element.dataset.floating) return;
      const x = parseFloat(element.style.getPropertyValue("--window-x"));
      const y = parseFloat(element.style.getPropertyValue("--window-y"));
      if (Number.isFinite(x) && Number.isFinite(y)) place(element, x, y);
    };
    const observer = new ResizeObserver(keepVisible);
    observer.observe(element);
    window.addEventListener("resize", keepVisible);

    const start = (event: globalThis.PointerEvent) => {
      const target = event.target;
      if (!event.isPrimary || event.button !== 0 || !(target instanceof Element) ||
          !target.closest(handles) || target.closest(controls) ||
          target.closest("[role=dialog], [role=alertdialog], dialog") !== element) return;
      const rect = element.getBoundingClientRect();
      drag.current = { id: event.pointerId, start: { x: event.clientX, y: event.clientY },
        offset: { x: event.clientX - rect.left, y: event.clientY - rect.top } };
      target.closest(handles)?.setPointerCapture(event.pointerId);
    };
    const move = (event: globalThis.PointerEvent) => {
      const active = drag.current;
      if (!active || active.id !== event.pointerId ||
          Math.hypot(event.clientX - active.start.x, event.clientY - active.start.y) < 4 && !element.dataset.floating) return;
      // Preserve the point under the pointer when a full-screen stage becomes a window.
      const width = element.offsetWidth;
      element.dataset.floating = "true";
      if (width && width !== element.offsetWidth) active.offset.x *= element.offsetWidth / width;
      place(element, event.clientX - active.offset.x, event.clientY - active.offset.y);
    };
    const finish = (event: globalThis.PointerEvent) => {
      if (drag.current?.id === event.pointerId) drag.current = null;
    };
    element.addEventListener("pointerdown", start);
    element.addEventListener("pointermove", move);
    element.addEventListener("lostpointercapture", finish);
    return () => {
      drag.current = null;
      observer.disconnect();
      window.removeEventListener("resize", keepVisible);
      element.removeEventListener("pointerdown", start);
      element.removeEventListener("pointermove", move);
      element.removeEventListener("lostpointercapture", finish);
      center(element);
    };
  }, []);

  function keys(event: KeyboardEvent<HTMLDivElement>) {
    const element = event.currentTarget.parentElement;
    if (!element) return;
    const steps: Record<string, Point> = { ArrowLeft: { x: -16, y: 0 }, ArrowRight: { x: 16, y: 0 },
      ArrowUp: { x: 0, y: -16 }, ArrowDown: { x: 0, y: 16 } };
    const step = steps[event.key];
    if (!step && event.key !== "Home") return;
    event.preventDefault();
    event.stopPropagation();
    if (!step) center(element);
    else {
      const rect = element.getBoundingClientRect();
      place(element, rect.left + step.x, rect.top + step.y);
    }
  }

  return <div className="window-grip" ref={grip} role="group" tabIndex={0} aria-label="Window position"
    aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown Home"
    title="Drag or use the arrow keys to move. Press Home to center."
    onKeyDown={keys} onDoubleClick={event => { if (event.currentTarget.parentElement) center(event.currentTarget.parentElement); }} />;
}

export function RoomDialogContent({ children, className, contentRef }: WindowProps & { contentRef?: Ref<HTMLDivElement> }) {
  return <DialogContent className={className} ref={contentRef} data-room-window="">{children}<WindowGrip /></DialogContent>;
}

export function RoomAlertContent({ children, className }: WindowProps) {
  return <AlertDialogContent className={className} data-room-window="">{children}<WindowGrip /></AlertDialogContent>;
}
