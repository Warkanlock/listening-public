import { currentAccount } from "./api";

const historyKey = () => `listening:music-searches:${currentAccount()}`;

export function recentMusicSearches(): string[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(historyKey()) || "[]");
    return Array.isArray(saved) ? saved.filter((q): q is string => typeof q === "string" && !!q.trim()).slice(0, 5) : [];
  } catch {
    return [];
  }
}

export function rememberMusicSearch(text: string) {
  const query = text.trim().slice(0, 160);
  if (!query || !currentAccount()) return;
  const others = recentMusicSearches().filter(q => q.toLowerCase() !== query.toLowerCase());
  try { localStorage.setItem(historyKey(), JSON.stringify([query, ...others].slice(0, 5))); }
  catch { /* Catalog searches still work when storage is unavailable. */ }
}
