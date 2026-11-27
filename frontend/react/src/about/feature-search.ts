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
  const controls = document.querySelector<HTMLElement>(".about-feature-search");
  const input = document.querySelector<HTMLInputElement>("#feature-query");
  const status = document.querySelector<HTMLElement>("#feature-status");
  const features = Array.from(document.querySelectorAll<HTMLDetailsElement>("#feature-results [data-feature]"));
  if (!controls || !input || !status || !features.length) return;

  const entries = features.map(feature => {
    const text = [feature.querySelector("summary")?.textContent, feature.querySelector("p")?.textContent,
      feature.dataset.keywords].filter(Boolean).join(" ");
    const words = normalize(text).match(/[\p{Letter}\p{Number}]+/gu) ?? [];
    return { feature, words };
  });
  const search = () => {
    const terms = normalize(input.value).match(/[\p{Letter}\p{Number}]+/gu) ?? [];
    let hasMatches = false;
    for (const { feature, words } of entries) {
      const matches = terms.every(term => words.some(word => near(term, word)));
      feature.hidden = !matches;
      hasMatches ||= matches;
    }
    status.textContent = hasMatches ? "" : "No features match. Try another search.";
  };
  input.addEventListener("input", search);
  controls.hidden = false;
  search();
}
