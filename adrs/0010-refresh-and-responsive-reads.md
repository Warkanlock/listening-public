# 0010. Publish catalog changes outside the playback request path

Accepted.

Pairing, session polling and streaming must remain responsive while a large library is scanned. The watchdog compares file and folder signatures to decide whether to sync inputs and rebuild. The audio folder participates in that decision so a newly supplied song can become available without editing its playlist. A refresh publishes one coherent catalog for readers. Repeated reads of an unchanged catalog do not rebuild it. [Refresh pipeline](../backend/system/refresh.py)

Normalize searchable fields once per catalog revision and paginate results before serializing track details. Worker status and external metadata checks have bounded costs. Negotiate gzip for JSON and static resources. Stream audio ranges without holding a catalog write lock. Threads serve requests while long jobs run in separately supervised processes. [Worker supervision](0001-supervisor-replaces-tmux.md)

The watchdog records a failure and continues subsequent cycles. Activity snapshots expose that history and the latest upkeep status to the Jobs view. A failed discovery or download request has its own result and can be retried without blocking the remaining queue.
