export function YouTubeLink({ artist, title }: { artist: string; title: string }) {
  const url = new URL("https://www.youtube.com/results");
  url.searchParams.set("search_query", [artist, title].filter(Boolean).join(" "));
  return (
    <a className="quiet-button" href={url.href} target="_blank" rel="noopener noreferrer">
      Find on YouTube
    </a>
  );
}
