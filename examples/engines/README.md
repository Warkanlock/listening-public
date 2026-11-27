# Listening add-ons

## Bring your own music sources.

Listening builds and plays a library from music files you already have. Add-ons let room operators connect a catalog, search for a recording, or fetch audio from a source they control. Start with the example catalog below and try it against your own files.

## Build a search or download engine

Listening asks an installed provider to search or fetch. The provider receives
one newline-terminated JSON request on stdin and writes one JSON response to
stdout. Keep diagnostics on stderr. A request includes `version` (currently 1)
and `action`. New workers include `"version":1` in every response alongside `ok` and the fields below. The manager also accepts older v1 responses that omit the response version. The host calls `/usr/local/bin/listening-engine` inside the
provider's own container. Its working filesystem is read-only. Use `/tmp`
for transient working files and `/credentials` for the provider's configuration.

| Action | Input | Response |
| --- | --- | --- |
| `test` | No extra fields | `{"ok":true}` when ready |
| `search` | `recording` with `id`, `title`, `search_title`, `artist`, `album`, `duration` in seconds, allowed `formats` such as `[".mp3",".flac"]`, and `preferred_format` such as `"mp3"` | `{"ok":true,"candidates":[{"source":"example-catalog","id":"first-light.mp3"}]}` |
| `fetch` | `candidate` with the same `source` and `id`, and a writable `staging` directory allocated for this request | `{"ok":true,"file":"first-light.mp3"}` identifying a complete regular file immediately inside staging |

While fetching, a worker can atomically replace `.download-progress` inside its staging directory with the whole percentage (ASCII digits, 0–100). Report percentages calculated from actual bytes and a known total. Progress is optional and the final reply still identifies only the completed audio file.

Return an empty candidate list for a search with no match. An operation failure
returns `{"ok":false,"retryable":true,"error":"Catalog temporarily unavailable"}`.
Use `retryable:false` when a candidate is permanently unavailable. If the
operation times out and its work has stopped, return
`{"ok":false,"retryable":true,"code":"timeout"}`. The room can try another
fetch candidate after that response. If every remaining candidate fails,
the timeout leaves the recording queued for retry. The host cancels operations when the caller disconnects or the deadline passes. Keep
each candidate ID stable, scoped to its source, and within 512 characters.
Return at most 20 candidates in preference order. The fetcher must check
whether it understands the candidate's source before attempting retrieval.

For authors using the Python SDK, implement `list(recording)`. The SDK maps it
to the existing `search` action. An individual request can include a `version`
such as `"Live"`. Candidates may include a public `label`, a
short `format`, and positive whole numbers for `duration` in seconds and
`bitrate` in kbps. Set `recommended` to false for uncertain alternatives.
Browse copies shows them and queues a user's selection with the original
source and ID kept on the server. Individual automatic requests skip
candidates explicitly marked uncertain.

The signed catalog entry declares the candidate namespaces for each capability
it offers. For example, the catalog worker declares
`"sources":{"search":["example-catalog"],"fetch":["example-catalog"]}`
and a fetch-only worker declares `"sources":{"fetch":["example-catalog"]}`.
A search-only entry lists just `search`, and a fetch-only entry lists just
`fetch`. A fetcher must accept every namespace advertised by the selected
searcher. Use the same namespace only for candidates with the same ID
format and meaning.

The host receives the final file after the provider operation exits. The provider
receives only its own scratch directory and credentials. Listening checks
free space, probes the returned bytes, and commits verified audio. No
provider response can grant playback eligibility.

### Optional sharing method

A plugin may declare `"share"` in its signed `capabilities`. Only `search` and `fetch` have source namespaces, so a share-only plugin declares `"sources":{}`. Declare settings for the sharing account in the same signed settings schema as other plugin settings.

The owner sends `POST /api/extensions` with `action: "share"`, the installed plugin ID in `engine`, and an `operation` of `status`, `start`, or `stop`. With `start`, `folder` names a directory relative to the operator's sharing root. An empty string selects the root. The reply includes `available`, `running`, and the saved relative `folder`. Running means the plugin process is alive.

The manager invokes `/usr/local/bin/listening-engine --share` as the foreground process in a separate container. The process receives the selected folder at `/share` and plugin settings at `/credentials`, both read-only. It can write to its own `/scratch` and `/tmp`. The plugin owns account validation and any external network protocol. The manager owns the process lifecycle.

## Temporary sign-in failures

If the provider cannot establish an authenticated session, the SDK can raise `LoginUnavailable(retry_after_seconds=1800)`. Workers that implement the protocol directly return:

```json
{"version":1,"ok":false,"retryable":true,"code":"login_unavailable","retry_after_seconds":1800}
```

The host pauses that provider’s readiness checks, searches, and transfers across the room, even after a restart. The optional delay accepts 1–86,400 whole seconds. If the field is missing or invalid, the host waits 30 minutes. Report an individual peer transfer timeout with `code:"timeout"`. Keep credentials and private provider diagnostics out of both responses.

## Give each plugin its own settings

Add a `settings` list to the signed catalog entry. Each field has a short `key`, human-readable `label`, a `type` of `text` or `secret`, and a Boolean `required` flag. For example, a catalog can declare a URL while an account-based plugin declares a username and password.

```json
{"key":"api_token","label":"Catalog access token","type":"secret","required":true}
```

The owner enters values under **Settings → Extensions → Configure**. The manager writes a private JSON object of field names to string values at `/credentials/plugin-settings.json` inside that worker. Saved values are never returned to the web app. A blank field keeps its saved value. The settings endpoint accepts a null value to remove that field. A worker's readiness check is responsible for reporting missing settings, including when an installation uses a legacy credentials file. Plugins with no settings declaration can continue to read their existing files.

## Get the example plugins

[Listening Plugins](https://github.com/Warkanlock/listening-plugins-public) contains implementations of this protocol. Its protocol driver dispatches `test`, `search`, and `fetch` to the installed implementation. Each plugin owns its query, matching, returned candidate IDs, and retrieval. The room can pair one plugin's search with another plugin's fetch when their signed source declarations match.

[Install a signed plugin catalog](../../docs/download-engines.md)
