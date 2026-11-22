# 0007. Share domain logic between the HTTP server and workers

Accepted.

The HTTP server and pipeline runner are separate processes. Both resolve audio, inspect the catalog, and update requests. Disagreement between them could make a song appear playable to one reader while the other refused it. Keep the Python backend on the standard library and give each shared rule one owning module.

| Responsibility | Owner |
| --- | --- |
| Request schema, queue transitions and lookup | [Requests store](../backend/downloads/requests_store.py) |
| Catalog identities, coverage and published build records | [Catalog store](../backend/catalog/catalog_store.py) and [catalog read model](../backend/catalog/catalog.py) |
| Schema setup, connection policy, busy timeout and state change stamps | [State database](../backend/system/state_database.py) |
| Room settings, derived paths and access to private configuration | [Configuration](../backend/system/config.py) |
| Atomic writes for host files, text normalization and supported extensions | [System package](../backend/system/) |
| File resolution, byte access and verification of blob ownership | [Catalog media modules](../backend/catalog/) |

An HTTP handler translates between the wire format and these operations. The runner calls the same operations. Importing the server module has no initialization side effects. This lets tests start a real listener against a temporary room.

SQLite replaced separate JSON and CSV state writes so catalog publication could be transactional across the server and its workers. Stable recording identifiers and catalog indexes survive rebuilds. A derived catalog can lose a list membership without losing the associated owned audio. The portable database and host database now have distinct ownership rules. [Collection ownership](0006-collection-ownership.md)
