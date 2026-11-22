import { RoomIcon } from "./RoomIcon";
import { Button } from "@ui-sdk/react/button";
import {
  createInputController,
  Input,
  InputGroup,
} from "@ui-sdk/react/input";

export type SearchController = ReturnType<typeof createInputController>;

// The topbar search field (parity item 4): typing filters the library
// live through the store's debounce, Enter or the icon searches the
// open catalogs, and the clear button resets the search.
export function SearchField({
  controller,
  value,
  onValueChange,
  onSearch,
  catalogSearch = true,
}: {
  controller: SearchController;
  value: string;
  onValueChange: (value: string) => void;
  onSearch: () => void;
  catalogSearch?: boolean;
}) {
  return (
    <div
      className="search-wrap"
      onKeyDown={(event) => {
        if (catalogSearch && event.key === "Enter") {
          event.preventDefault();
          onSearch();
        }
      }}
    >
      {catalogSearch && <Button
        className="search-icon"
        aria-label="Search open music catalogs"
        title="Search open music catalogs"
        onClick={onSearch}
      >
        <RoomIcon name="search" />
      </Button>}
      <InputGroup className="search-control">
        <Input
          type="search"
          autoComplete="off"
          placeholder="Search library"
          aria-label="Search songs, artists and albums"
          aria-describedby="searchGuide"
          value={value}
          onValueChange={onValueChange}
          controller={controller}
        />
        {value ? (
          <Button
            className="search-clear"
            aria-label="Clear search"
            onClick={() => {
              controller.clear();
              onValueChange("");
              controller.focus();
            }}
          >
            <RoomIcon name="close" />
          </Button>
        ) : null}
      </InputGroup>
      <kbd aria-hidden="true">{/Mac|iPad|iPhone/.test(navigator.platform) ? "⌘ K" : "Ctrl K"}</kbd>
      <span id="searchGuide" className="search-guide">
        {catalogSearch ? "Type to filter your library. Press Enter to search open catalogs." : "Type to filter the room library."}
      </span>
    </div>
  );
}
