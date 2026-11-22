# Background work waits in a durable job queue

Starting a job used to be imperative: pressing Start spawned the worker immediately, and if another worker held the library the answer was a blocker, not a place in line. The console's record of what it had started lived in the server's memory and died with it. We decided (owner, 2026-11-17, folded into plan 050 Step 2 alongside ADR 0001) that the server keeps a durable queue of jobs in the state database: kind, scope, state, and enqueue, start, and finish times. Start enqueues; the watchdog becomes a dispatcher that starts the next queued job whenever no worker is running; Stop on a waiting job cancels it, and Stop on the running job keeps its current meaning, including bulk stop disarming auto-download. The song requests queue is untouched: it queues songs, which are user data, not work.

## Considered options

- **Keep imperative starts.** Simplest, and the mutual-exclusion blocker is honest. Rejected: "wait your turn" is the normal shape of background work, and a personal server is exactly where jobs should pile up quietly instead of erroring.
- **An external queue (Redis, RQ, Celery).** Real tooling, wrong scale: a new service to install and supervise, against the project's standard-library-only rule.
- **An in-memory queue.** Fixes the blocker, keeps the amnesia. The queue's value is precisely that it survives the server.

## Consequences

- Job cards gain a queued state (and position) between idle and running.
- The watchdog's standing duties (draining song requests, auto-download when armed) remain, as the dispatcher's fallback when the queue is empty.
- A queued job whose blocker has appeared by dispatch time (setup unfinished, list deleted) fails visibly on its card instead of starting; it never retries silently.
