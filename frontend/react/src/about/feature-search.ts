export type PatternResult = { kind: "matches"; indexes: number[] } | { kind: "invalid" };

function validResult(data: unknown): data is PatternResult {
  if (!data || typeof data !== "object" || !("kind" in data)) return false;
  if (data.kind === "invalid") return true;
  return data.kind === "matches" && "indexes" in data && Array.isArray(data.indexes) &&
    data.indexes.every(index => Number.isInteger(index) && index >= 0);
}

function normalize(text: string) {
  return text.toLowerCase().normalize("NFKD").replace(/\p{Mark}/gu, "");
}

function near(term: string, word: string) {
  if (word.includes(term)) return true;
  const edits = term.length >= 7 ? 2 : term.length >= 4 ? 1 : 0;
  if (edits === 0 || Math.abs(word.length - term.length) > edits) return false;
  let previous = Array.from({ length: word.length + 1 }, (_, index) => index);
  let beforePrevious = previous;
  for (let row = 1; row <= term.length; row++) {
    const current = [row];
    for (let col = 1; col <= word.length; col++) {
      current[col] = Math.min(current[col - 1] + 1, previous[col] + 1,
        previous[col - 1] + Number(term[row - 1] !== word[col - 1]));
      if (row > 1 && col > 1 && term[row - 1] === word[col - 2] && term[row - 2] === word[col - 1]) {
        current[col] = Math.min(current[col], beforePrevious[col - 2] + 1);
      }
    }
    beforePrevious = previous;
    previous = current;
  }
  return previous[word.length] <= edits;
}

export function enableFeatureSearch() {
  const form = document.querySelector<HTMLElement>(".about-feature-search");
  const input = document.querySelector<HTMLInputElement>("#feature-query");
  const pattern = document.querySelector<HTMLInputElement>("#feature-pattern");
  const status = document.querySelector<HTMLElement>("#feature-status");
  const features = Array.from(document.querySelectorAll<HTMLDetailsElement>("#feature-results [data-feature]"));
  if (!form || !input || !pattern || !status || !features.length) return;

  const entries = features.map(feature => {
    const text = [feature.querySelector("summary")?.textContent, feature.querySelector("p")?.textContent,
      feature.dataset.keywords].filter(Boolean).join(" ");
    return { text, words: normalize(text).match(/[\p{Letter}\p{Number}]+/gu) ?? [] };
  });
  let worker: Worker | undefined;
  let deadline: number | undefined;
  function finish() {
    worker?.terminate();
    worker = undefined;
    window.clearTimeout(deadline);
  }
  const display = (matches: number[]) => {
    features.forEach((feature, index) => { feature.hidden = !matches.includes(index); });
    status.textContent = matches.length === 0 ? "No features match. Try another search."
      : `${matches.length} ${matches.length === 1 ? "feature" : "features"}`;
  };
  const search = () => {
    finish();
    const query = pattern.checked ? input.value : input.value.trim();
    input.removeAttribute("aria-invalid");
    const reportError = (message: string, invalid = false) => {
      finish();
      if (invalid) input.setAttribute("aria-invalid", "true");
      features.forEach(feature => { feature.hidden = true; });
      status.textContent = message;
    };
    if (query.length > 160) {
      reportError("Use 160 characters or fewer.", true);
      return;
    }
    if (!query || !pattern.checked) {
      const terms = normalize(query).match(/[\p{Letter}\p{Number}]+/gu) ?? [];
      display(entries.flatMap(({ words }, index) =>
        terms.every(term => words.some(word => near(term, word))) ? [index] : []));
      return;
    }

    try {
      const request = new Worker(new URL("./feature-pattern.worker.ts", import.meta.url), { type: "module" });
      worker = request;
      status.textContent = "Searching…";
      request.onmessage = ({ data }: MessageEvent<unknown>) => {
        if (worker !== request) return;
        if (!validResult(data)) {
          reportError("Pattern search is unavailable. Try text search.");
          return;
        }
        if (data.kind === "invalid") reportError("Check your regular expression.", true);
        else {
          finish();
          display(data.indexes);
        }
      };
      request.onerror = () => { if (worker === request) reportError("Pattern search is unavailable. Try text search."); };
      deadline = window.setTimeout(() => {
        if (worker === request) reportError("That pattern took too long. Try a simpler expression.", true);
      }, 600);
      request.postMessage({ query, texts: entries.map(entry => entry.text) });
    } catch {
      reportError("Pattern search is unavailable. Try text search.");
    }
  };
  input.addEventListener("input", search);
  pattern.addEventListener("change", search);
  form.hidden = false;
  search();
}
