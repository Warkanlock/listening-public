import { RoomIcon } from "./RoomIcon";

export function YouTubeLink({ artist, title, iconOnly = false }: { artist: string; title: string; iconOnly?: boolean }) {
  const url = new URL("https://www.youtube.com/results");
  url.searchParams.set("search_query", [artist, title].filter(Boolean).join(" "));
  return (
    <a className={`quiet-button${iconOnly ? " icon-control" : ""}`} href={url.href}
      aria-label="Find on YouTube" title={iconOnly ? "Find on YouTube" : undefined} target="_blank" rel="noopener noreferrer">
      {iconOnly ? <RoomIcon name="northeast" /> : "Find on YouTube"}
    </a>
  );
}
