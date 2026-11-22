import { RoomIcon } from "./RoomIcon";
import { useEffect, useRef, useState } from "react";
import { useAsyncOptions } from "@ui-sdk/react/async-options";
import {
  Combobox,
  ComboboxChip,
  ComboboxChipRemove,
  ComboboxChips,
  ComboboxClear,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxLabel,
  ComboboxList,
  ComboboxValue,
} from "@ui-sdk/react/combobox";
import { useStore } from "../store";

const MAX_SUGGESTIONS = 8;

// Suggestions derive locally from the latest /api/tracks facet, the
// way the vanilla control derives them: untagged genres only, prefix
// matches before substring matches, facet order inside each group,
// at most eight shown.
function deriveSuggestions(facet: string[], tagged: string[], draft: string) {
  const untagged = facet.filter((genre) => !tagged.includes(genre));
  const needle = draft.toLowerCase();
  const candidates = needle
    ? [
        ...untagged.filter((genre) =>
          genre.toLowerCase().startsWith(needle),
        ),
        ...untagged.filter(
          (genre) =>
            !genre.toLowerCase().startsWith(needle) &&
            genre.toLowerCase().includes(needle),
        ),
      ]
    : untagged;
  return {
    items: candidates.slice(0, MAX_SUGGESTIONS),
    more: candidates.length > MAX_SUGGESTIONS,
  };
}

// The genre tag autocomplete (plan 047, item 16): a multiple
// combobox whose value is the store's tag array. Choosing toggles a
// tag and reloads the tracks once; the shipped reset rule clears
// the tags from the store when a changed search query commits.
export function GenreTagFilter() {
  const store = useStore();
  const facetRef = useRef(store.genreOptions);
  facetRef.current = store.genreOptions;
  const taggedRef = useRef(store.genres);
  taggedRef.current = store.genres;
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [field, setField] = useState<HTMLDivElement | null>(null);

  const options = useAsyncOptions<string>(
    async (query) => {
      const derived = deriveSuggestions(
        facetRef.current,
        taggedRef.current,
        query,
      );
      setHasMore(derived.more);
      return derived.items;
    },
    { immediate: true, debounce: 0 },
  );
  const reloadRef = useRef(options.reload);
  reloadRef.current = options.reload;
  const setQueryRef = useRef(options.setQuery);
  setQueryRef.current = options.setQuery;

  // The facet and the tag set move outside the draft (a tracks load
  // lands, a tag is added or cleared); re-derive around the draft.
  useEffect(() => {
    reloadRef.current();
  }, [store.genreOptions, store.genres]);

  // A committed query change is the reset rule's moment: the store
  // has already cleared the tags; the draft and the list follow.
  const committedQuery = useRef(store.query);
  useEffect(() => {
    if (committedQuery.current === store.query) return;
    committedQuery.current = store.query;
    setDraft("");
    setOpen(false);
    setQueryRef.current("");
  }, [store.query]);

  const emptyMessage = (text: string) => {
    if (!store.genreOptions.length) return "No genres available yet";
    if (text) {
      const tagged = store.genreOptions.find(
        (genre) =>
          genre.toLowerCase() === text.toLowerCase() &&
          store.genres.includes(genre),
      );
      if (tagged) return `"${tagged}" is already added`;
      return `No genres match "${text}"`;
    }
    return "All genres are added";
  };

  return (
    <div className="genre-filter">
      <Combobox
        multiple
        value={store.genres}
        onValueChange={(next) => store.setGenres(next)}
        items={options.items}
        status={options.status}
        filter={false}
        autoHighlight
        itemToLabel={(genre) => genre}
        query={draft}
        onQueryChange={(query) => {
          setDraft(query);
          options.setQuery(query);
        }}
        open={open}
        onOpenChange={setOpen}
      >
        <ComboboxLabel>Genre</ComboboxLabel>
        <div className="genre-tag-field" ref={setField}>
          <ComboboxChips>
            <ComboboxValue>
              {(state) =>
                (state.value as string[]).map((genre) => (
                  <ComboboxChip key={genre} value={genre}>
                    {genre}
                    <ComboboxChipRemove aria-label={`Remove ${genre}`}>
                      <span aria-hidden="true"><RoomIcon name="close" /></span>
                    </ComboboxChipRemove>
                  </ComboboxChip>
                ))
              }
            </ComboboxValue>
            <ComboboxInput
              placeholder="Add a genre"
              aria-label="Add a genre filter"
              autoComplete="off"
              spellCheck={false}
              onKeyDown={(event) => {
                // Escape with the list closed clears the draft; with
                // it open the combobox closes the list itself.
                if (event.key === "Escape" && !open && draft) {
                  event.preventDefault();
                  setDraft("");
                  options.setQuery("");
                }
              }}
            />
          </ComboboxChips>
          <ComboboxClear className="genre-clear">Clear</ComboboxClear>
        </div>
        <ComboboxContent anchor={field}>
          <ComboboxEmpty>
            {(state) =>
              state.empty && !state.loading ? emptyMessage(state.query) : null
            }
          </ComboboxEmpty>
          <ComboboxList>
            {(state) =>
              (state.items as string[]).map((genre) => (
                <ComboboxItem key={genre} value={genre} label={genre}>
                  {genre}
                </ComboboxItem>
              ))
            }
          </ComboboxList>
          {hasMore ? (
            <div className="genre-suggestion-hint">
              Keep typing to narrow the list
            </div>
          ) : null}
        </ComboboxContent>
      </Combobox>
    </div>
  );
}
