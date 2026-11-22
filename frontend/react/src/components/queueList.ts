import {
  createChange,
  createReorder,
  createRovingFocus,
  definePrimitive,
  type ChangeDetails,
  type ReorderDraft,
} from "@ui-sdk/core";

// The queue list primitive (item 31): the committed order is a list
// of item keys owned by the caller (the shared session); a move runs
// as a draft through createReorder and commits on drop through
// onValueChange. Items carry nested controls, so the drag and
// keyboard surface is a separate handle part, as core.md's
// Reordering section prescribes; the item part itself binds the
// reorder so the row receives the drag translation variables.
export type QueueListOptions = {
  value?: string[];
  defaultValue?: string[];
  onValueChange?: (
    value: string[],
    details: ChangeDetails<"drag" | "move-key" | "imperative">,
  ) => void;
  itemLabel?: (key: string) => string | undefined;
};

type QueueListState = {
  value: string[];
  moving: ReorderDraft | null;
  focused: string | null;
};

export const queueList = definePrimitive({
  name: "queue-list",
  options: {
    value: "value",
    defaultValue: "value",
    onValueChange: "callback",
    itemLabel: "value",
  },
  controlled: ["value"],
  state: (options: QueueListOptions): QueueListState => ({
    value: options.value ?? options.defaultValue ?? [],
    moving: null,
    focused: null,
  }),
  sync: (options: QueueListOptions) =>
    options.value === undefined ? {} : { value: options.value },
  api: (context) => {
    const change = createChange(context);
    const reorder = createReorder(context, {
      slot: "item",
      key: "value",
      draft: "moving",
      set: (value, details) => change("value", value, details),
      label: (key) => context.options().itemLabel?.(key),
    });
    const roving = createRovingFocus(context, {
      slot: "handle",
      key: "focused",
    });
    // The parts machinery reads api.internal.keyOf whenever a part
    // with a "key" prop renders inside another part (the item inside
    // the root, the handle inside the item) to derive the part's id.
    // Built-in primitives supply it in their api factory; a custom
    // primitive must too, or the first keyed part throws and unmounts
    // the whole app. Queue keys are already strings.
    const internal = {
      keyOf: (value: unknown) => (value == null ? "" : String(value)),
    };
    return { reorder, roving, internal };
  },
  slots: (slot) => ({
    root: slot<{}, { order: string[] }>({
      state: (state, { api }) => ({ order: api.reorder.order(state) }),
    }),
    item: slot<{ value: string }>({
      props: { value: "key" },
      attrs: (state, { api, key }) => ({
        "data-grabbed": api.reorder.moving(state, key) ? "" : undefined,
        "aria-roledescription": "sortable item",
      }),
      bind: (el, part) => part.api.reorder.bind(el, part),
    }),
    handle: slot<{ value: string }>({
      props: { value: "key" },
      attrs: (state, part) => ({
        type: "button",
        tabindex: part.api.roving.tabIndex(state, part),
      }),
      on: {
        keydown: (event, { el, api, key }) => {
          if (el) api.reorder.keydown(event, key) || api.roving.keydown(event, el);
        },
        pointerdown: (event, { api, key }) => api.reorder.pointerdown(event, key),
      },
      bind: (el, part) => part.api.roving.bind(el, part),
    }),
  }),
});
