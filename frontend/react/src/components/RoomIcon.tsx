import type { ComponentPropsWithRef } from "react";

const paths = {
  back: "M20 12H4m7-7-7 7 7 7",
  forward: "M4 12h16m-7-7 7 7-7 7",
  northeast: "M5 19 19 5M7 5h12v12",
  refresh: "M20 10a8 8 0 10.2 5M20 4v6h-6",
  stop: "M6 6h12v12H6z",
  info: "M12 3a9 9 0 100 18 9 9 0 000-18z M12 11v6m0-10v.1",
  alert: "M12 3 2 21h20z M12 9v5m0 3v.1",
  album: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  music: "M9 17V6l11-3v11M9 10l11-3M9 17a3 3 0 11-3-3h3m11 0a3 3 0 11-3-3h3",
  volume: "M3 9h4l5-5v16l-5-5H3z M16 8a6 6 0 010 8m3-11a10 10 0 010 14",
  more: "M5 12h.1m6.9 0h.1m6.9 0h.1",
  chevronUp: "m6 15 6-6 6 6",
  chevronRight: "m9 6 6 6-6 6",
  up: "M12 20V4m-7 7 7-7 7 7",
  down: "M12 4v16m-7-7 7 7 7-7",
  grip: "M8 5h.1m7.9 0h.1M8 12h.1m7.9 0h.1M8 19h.1m7.9 0h.1",
  listen: "M4 16v-4a8 8 0 0116 0v4 M4 13h3v7H5a1 1 0 01-1-1z M20 13h-3v7h2a1 1 0 001-1z",
  library: "M4 4h4v16H4z M11 4h3v16h-3z M17 5l3-1 3 15-3 1z",
  discover: "M12 2l2.6 7.4L22 12l-7.4 2.6L12 22l-2.6-7.4L2 12l7.4-2.6z",
  insights: "M3 19h18M5 15l4-5 4 3 6-8M16 5h3v3",
  camping: "M2 20h20M4 20L12 5l8 15M10.5 8l3-5M9 20l3-6 3 6",
  lyrics: "M4 4h16v12H10l-5 4v-4H4z M8 8h8 M8 12h5",
  radio: "M3 10h18v10H3z M4 10l15-6 M7 15h3m5 0h3m-3 3h3",
  catalogs: "M12 2a10 10 0 100 20 10 10 0 000-20z M15.5 8.5l-2 5-5 2 2-5z",
  extensions: "M8 3v4m8-4v4M6 7h12v3a6 6 0 01-12 0z M12 16v5",
  requests: "M12 3a9 9 0 100 18 9 9 0 000-18z M8 12h8m-4-4v8",
  books: "M12 6c-3-2-6-2-9-1v14c3-1 6-1 9 1 3-2 6-2 9-1V5c-3-1-6-1-9 1zm0 0v14",
  podcasts: "M9 5a3 3 0 116 0v7a3 3 0 11-6 0z M5 10v2a7 7 0 0014 0v-2 M12 19v3m-3 0h6",
  playlists: "M3 5h14M3 10h14M3 15h8m5 2v-5l5-1v6a2.5 2.5 0 11-2-2",
  jobs: "M4 5h16v14H4z M8 9h8m-8 6h4m4 0h1",
  settings: "M4 6h16M4 12h16M4 18h16 M8 4v4m8 2v4m-6 2v4",
  favorites: "M12 20l-8-8a4.5 4.5 0 016-7l2 2 2-2a4.5 4.5 0 016 7z",
  recent: "M12 3a9 9 0 100 18 9 9 0 000-18z M12 7v6h4",
  most: "M7 8h12l-3-3 M17 16H5l3 3 M5 16v-2m14-6v2",
  moods: "M5 16l5-9 8 4-3 8-10-3z M5 16h0m5-9h0m8 4h0m-3 8h0",
  people: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-4m-5-8a4 4 0 1 1-8 0 4 4 0 0 1 8 0m3-4a4 4 0 0 1 0 8",
  devices: "M8 3h8a2 2 0 012 2v14a2 2 0 01-2 2H8a2 2 0 01-2-2V5a2 2 0 012-2z M11 18h2",
  download: "M12 3v12m-4-4 4 4 4-4 M4 16v4h16v-4",
  inbox: "M3 13l3-8h12l3 8v6H3z M3 13h5l2 3h4l2-3h5",
  announcements: "M3 6h18v12H3z M3 7l9 7 9-7",
  help: "M12 3a9 9 0 100 18 9 9 0 000-18z M9.5 9a2.5 2.5 0 115 0c0 1.4-2.5 1.8-2.5 3.5M12 16h.01",
  menu: "M4 7h16M4 12h16M4 17h16",
  power: "M12 3v9M7 5a8 8 0 1 0 10 0",
  undo: "M9 4 4 9l5 5M4 9h10a5 5 0 0 1 0 10h-4",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5m4-5v5",
  close: "M6 6l12 12M18 6L6 18",
  outputs: "M3 5h15v10H3z M8 19h5 M10.5 15v4 M17 10h4v10h-4z",
  play: "M8 5l11 7-11 7z",
  pause: "M8 5v14M16 5v14",
  add: "M12 5v14M5 12h14",
  search: "M16 16l5 5M18 10a8 8 0 11-16 0 8 8 0 0116 0z",
  magnet: "M5 3h4v10a3 3 0 006 0V3h4v10a7 7 0 01-14 0z M5 7h4m6 0h4",
  share: "M12 15V3m-4 4 4-4 4 4 M6 11H4v10h16V11h-2",
  chevronDown: "m6 9 6 6 6-6",
  check: "m5 12 4 4L19 6",
  shuffle: "M3 6h2c5 0 7 12 12 12h4m-4-4 4 4-4 4M3 18h2c2 0 3-2 5-5m4-4c1-2 2-3 4-3h3m-4-4 4 4-4 4",
  previous: "M5 6v12m14-12L8 12l11 6z",
  next: "M19 6v12M5 6l11 6-11 6z",
} satisfies Record<string, string>;

export type RoomIconName = keyof typeof paths;

export function RoomIcon({ name }: { name: RoomIconName }) {
  return <svg className="room-icon" width="20" height="20" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"
    strokeLinejoin="round" aria-hidden="true" focusable="false">
    <path d={paths[name]} />
  </svg>;
}

export function RoomIconButton({ icon, label, primary = false, ...props }:
  Omit<ComponentPropsWithRef<"button">, "children" | "className"> &
  { icon: RoomIconName; label: string; primary?: boolean }) {
  const appearance = primary ? "primary-action icon-control" : "quiet-button icon-control";
  return <button type="button" className={appearance}
    aria-label={label} title={label} {...props}><RoomIcon name={icon} /></button>;
}
