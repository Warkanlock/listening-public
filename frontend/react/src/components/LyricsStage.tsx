import { RoomIcon } from "./RoomIcon";
import { RoomDialogContent } from "./WindowContent";
import { useEffect, useRef, useState } from "react";
import {
  Dialog,
  DialogBackdrop,
  DialogClose,
  DialogTitle,
} from "@ui-sdk/react/dialog";
import { coverUrl, scrollBehavior } from "../api";
import { lyricRef, lyricsNote, useLyrics } from "../lyrics";
import { useStore } from "../store";

export interface LyricsSubject {
  id: number | string;
  title: string;
  artist: string;
  cover: string;
}

// The lyrics stage (plan 049): a full-viewport overlay above every
// other surface. The song's cover, blurred under a deep tint, backs
// large tracked lines; the current line sits near the vertical center
// at full strength, and clicking a line seeks when the stage's song is
// the one playing. Plain lyrics render in the same type with no
// tracking and no seek. Data is the dialog's old /api/lyrics payload,
// unchanged; only the presentation moved.
export function LyricsStage() {
  const store = useStore();
  const origin = store.lyricsSubject;
  const following = origin === "radio" || origin === "room";
  const session = store.session;
  const radio = store.radioSounding ? store.radioSlice.lyricTrack : null;
  const inRadio = origin === "radio" || (!following && !!radio && radio.id === origin?.id);
  const current = inRadio ? radio : session?.current;
  const subject = following
    ? current && lyricRef(current.id) ? { id: current.id, title: current.title, artist: current.artist, cover: current.cover || "" } : null
    : origin;
  const id = subject?.id ?? null;
  useEffect(() => {
    if (following && id === null) store.closeLyrics();
  }, [following, id, store]);
  const { outcome } = useLyrics(id);
  const scrollRef = useRef<HTMLDivElement>(null);
  const currentRef = useRef<HTMLButtonElement | null>(null);
  const isCurrentSong = !!subject && current?.id === id;
  const seekable = isCurrentSong && (!inRadio || store.radioSlice.hasNext);
  const [position, setPosition] = useState(0);
  useEffect(() => {
    if (id === null || !isCurrentSong || outcome?.kind !== "synced") return;
    const sample = () => {
      const session = store.session;
      const local = !store.radioSounding && session?.playing ? store.sessionSlice.readMediaPosition?.() : undefined;
      setPosition(inRadio && typeof id === "number" ? store.radioControls?.libraryTime(id) ?? store.radioSlice.restoredPosition ?? 0
        : local ?? session?.position ?? 0);
    };
    sample();
    const timer = window.setInterval(sample, 250);
    return () => window.clearInterval(timer);
  }, [id, inRadio, isCurrentSong, outcome, store]);
  const lines = outcome?.kind === "synced" ? outcome.lines : null;
  const currentIndex =
    lines && isCurrentSong
      ? lines.reduce(
          (active, line, index) =>
            line.time <= (position || 0) ? index : active,
          -1,
        )
      : -1;

  // Keep the current line near the vertical center of the stage. The
  // container scrolls itself (never scrollIntoView, which would also
  // move the dialog underneath), and the shared scrollBehavior helper
  // already answers "auto" under prefers-reduced-motion.
  useEffect(() => {
    if (currentIndex < 0) return;
    const container = scrollRef.current;
    const line = currentRef.current;
    if (!container || !line) return;
    container.scrollTo({
      top: line.offsetTop - container.clientHeight / 2 + line.clientHeight / 2,
      behavior: scrollBehavior(),
    });
  }, [currentIndex, lines]);

  if (!subject) return null;
  const backdrop = coverUrl(subject.cover);
  const plainLines =
    outcome?.kind === "plain" ? outcome.text.split("\n") : null;
  const note = lyricsNote(outcome);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) {
          store.closeLyrics();
        }
      }}
    >
      <DialogBackdrop />
      <RoomDialogContent
        className={`lyrics-stage${seekable && lines ? " is-seekable" : ""}`}
      >
        <div className="lyrics-stage-backdrop" aria-hidden="true">
          {backdrop ? <img src={backdrop} alt="" /> : null}
        </div>
        <div className="lyrics-stage-head">
          <div>
            <DialogTitle className="lyrics-stage-title">
              {subject.title}
            </DialogTitle>
            <p className="lyrics-stage-artist">{subject.artist}</p>
          </div>
          <DialogClose className="dialog-close" aria-label="Close lyrics">
            <RoomIcon name="close" />
          </DialogClose>
        </div>
        <div className="lyrics-stage-lines" ref={scrollRef}>
          {outcome === null ? (
            <p className="lyrics-stage-note">Looking for lyrics…</p>
          ) : note ? (
            <p className="lyrics-stage-note">{note}</p>
          ) : lines ? (
            lines.map((line, index) => (
              <button
                key={`${line.time}-${index}`}
                type="button"
                disabled={!seekable}
                className={`lyrics-stage-line${index === currentIndex ? " is-current" : ""}`}
                data-t={line.time}
                ref={
                  index === currentIndex
                    ? (element) => {
                        currentRef.current = element;
                      }
                    : undefined
                }
                onClick={() => {
                  if (seekable) {
                    if (inRadio && typeof subject.id === "number") store.radioControls?.seekLibrary(subject.id, line.time);
                    else void store.sendSession("seek", { position: line.time, expected_ref: subject.id });
                  }
                }}
              >
                {line.text || "♪"}
              </button>
            ))
          ) : plainLines ? (
            plainLines.map((text, index) => (
              <span key={index} className="lyrics-stage-line">
                {text}
              </span>
            ))
          ) : null}
        </div>
      </RoomDialogContent>
    </Dialog>
  );
}
