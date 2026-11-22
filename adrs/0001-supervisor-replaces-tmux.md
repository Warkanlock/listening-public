# Workers run as supervisor-managed processes, not tmux sessions

Until now the server started, stopped, and checked its four workers (bulk downloads, song requests, genre categorization, sharing) by shelling out to tmux: `new-session` to start, `send-keys C-c` to stop, `has-session` to check. That made a terminal multiplexer a hard dependency of the product: no tmux on PATH meant no workers, and the Jobs console reported that fact to the user as a blocker. We decided (owner grill, 2026-11-17) that the server supervises the workers itself: each worker is a detached subprocess in its own process group, with a pidfile and log in the application folder, re-adopted by pidfile when the server restarts. Stop is SIGINT to the process group, escalating to SIGKILL after 10 seconds. The jobs API and the watchdog's decisions do not change; only the process layer under them does.

## Considered options

- **Keep tmux.** It works, and a human can attach to a session. Rejected: it is a system dependency the product cannot install or explain, its subprocess calls carry no timeouts (plan 038 follow-up F3), and attaching is a developer habit, not a user feature. The log files and the Jobs console cover the legitimate need.
- **systemd user units on Linux, launchd on macOS.** Native and robust, but two mechanisms for one behavior, and the room must behave identically on both. Rejected in favor of one small supervisor module in the server.

## Consequences

- tmux leaves the dependency checks and the user-facing blocker copy.
- Workers survive a server restart by re-adoption, the one property tmux genuinely provided.
- A worker started by an older, tmux-era server is not adopted; the upgrade path is to stop workers before updating, which the release notes state.
