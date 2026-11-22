# AGENTS.md

Working rules for anyone (human or agent) changing this repository.

## Layout

- `backend/` is a domain-organized Python package tree. Entry points are
  `backend/server.py` (HTTP) and `backend/runner.py` (pipeline); domain
  code lives in the package that owns it (`library/`, `catalog/`,
  `playback/`, `downloads/`, `metadata/`, `adapters/`, `devices/`,
  `system/`). New modules land in their domain package, never at the
  repository root.
- `frontend/` holds the web app: `frontend/react/` is the React 19 +
  TypeScript app on @ui-sdk/react, built by Vite into
  `frontend/dist/`, and the dist output is what the server serves
  ([ADR 0014](adrs/0014-web-client-and-rendered-verification.md)).
  Before using ui-sdk, follow the ui-sdk skill if it's installed, and read node_modules/@ui-sdk/react/docs/llms.txt. To build a new component, also search node_modules/@ui-sdk/react/docs/core.md by heading, and read rendering.md next to it.
- Entry points run as scripts (`python3 backend/server.py`), which puts
  `backend/` on `sys.path`; domain packages import as top-level packages
  (`from library.library_protocol import ...`).

## Rules

- Python standard library only. The frontend is a React 19 +
  TypeScript app on @ui-sdk/react in `frontend/react/`, built by Vite
  into `frontend/dist/`; the design tokens in its `styles.css` are
  shared across the interface.
- The verification invariant is the product: a download counts only
  after ffprobe confirms real audio, at least 192 kbps, duration within
  5.5 seconds of expected, and a matching title tag. Do not weaken it.
- Credentials and the pairing token live in the user's app config
  folder, outside this repository. Never commit secrets, databases, or
  workspace data.
- Verify before declaring done: `python3 -m unittest discover -s tests`
  and `python3 -m py_compile` over the backend tree must be green, and
  a real server boot must answer `/` and one `/api/` route.
- UI work is gated: `yarn build` runs the class contract
  check (`frontend/react/tools/check-classes.mjs`), which fails when
  a class name used in the TSX has no rule in `styles.css`, and
  `make verify-ui` drives the built interface in a headless browser
  against a fixture room. A failing gate blocks the push unless the
  owner personally waives it; pushes of UI changes cite the class
  check and `make verify-ui` output.
- Every push is preceded by a code review (owner's rule, 2026-11-17):
  the code-review skill's two axes, Standards (this file, GLOSSARY.md,
  and the smell baseline) and Spec (the requested behavior and applicable decision records),
  run as separate fresh-context reviewers over the change's diff
  against its base commit. Hard findings are fixed before the push;
  the push report cites the review outcome.
- Tests cover the app (backend and its behavior). The setup and
  migration tooling (`scripts/`) carries no unit tests:
  those scripts verify themselves with built-in checks (refusals,
  post-move verification) and are exercised by manual smokes. Owner
  decision, 2026-11-16.
- Architecture decisions live in [adrs/](adrs/README.md). Its index maps
  older plan numbers to the current records. Executors work within their
  assigned scope, leave pushes to the coordinator, and report conflicts
  between the requested behavior and the existing code.
