#!/bin/sh
# Listening Room: bootstrap install (plan 044), the fallback route
# for a Mac or Linux box without Homebrew.
#
# Run this script with Git access to the repository. It builds a private
# install from main. An existing installation must fast-forward cleanly.
# --dry-run prints the sequence without changing the system.
set -eu

REPO_URL="${LISTENING_SOURCE_REPO:-}"
RELEASE_BRANCH="${LISTENING_SOURCE_BRANCH:-main}"
DEST="${LISTENING_INSTALL_DIR:-$HOME/listening-room}"
DRY=0
DOWNLOADS=0

say() { printf '%s\n' "$*"; }
fail() { printf '%s\n' "$*" >&2; exit 1; }
would() { say "  would: $*"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --downloads) DOWNLOADS=1 ;;
    --repo)
      [ "$#" -ge 2 ] || fail "Supply a Git URL after --repo."
      REPO_URL="$2"
      shift ;;
    --help)
      say "Usage: bootstrap.sh [--repo GIT_URL] [--downloads] [--dry-run]"
      say "Install from the checkout's origin or specify a private Git URL."
      exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
  shift
done
if [ -z "$REPO_URL" ]; then
  SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
  REPO_URL=$(git -C "$SCRIPT_DIR" config --get remote.origin.url 2>/dev/null || true)
fi
[ -n "$REPO_URL" ] || fail "Pass --repo with the private Git repository URL."
case "$REPO_URL" in -*) fail "Supply a Git URL, not a command option." ;; esac

OS=$(uname -s)
case "$OS" in
  Darwin|Linux) ;;
  *) fail "bootstrap.sh covers macOS and Linux. On this platform, install Listening Room through the Docker route instead: docker compose up -d, from the docker-compose.yml in the $RELEASE_BRANCH release." ;;
esac

say "Listening Room bootstrap: install $RELEASE_BRANCH into $DEST."
[ "$DRY" = 1 ] && say "(dry run: printing the plan, changing nothing)"

# 1. Python 3.9 or newer: every route needs it, and nothing here
# installs a Python.
if ! command -v python3 >/dev/null 2>&1 \
   || ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  fail "Python 3.9 or newer is required and was not found. Install it (macOS: brew install python; Debian/Ubuntu: sudo apt install python3), then run this script again."
fi
[ "$DRY" = 1 ] && would "use $(python3 --version 2>&1) ($(command -v python3))"

# 2. System tools: ffprobe (ffmpeg). This route does not
# install system package managers: on macOS it installs through
# the Homebrew the owner already has and stops with the exact line
# when Homebrew is absent; on Debian/Ubuntu it prints the apt line
# and stops, the server_setup.sh discipline.
MISSING=''
command -v ffprobe >/dev/null 2>&1 || MISSING="$MISSING ffmpeg"
if [ -n "$MISSING" ]; then
  if [ "$OS" = Darwin ]; then
    if command -v brew >/dev/null 2>&1; then
      if [ "$DRY" = 1 ]; then
        would "brew install$MISSING"
      else
        say "Installing the missing system tools with Homebrew:$MISSING"
        # shellcheck disable=SC2086
        brew install $MISSING
      fi
    else
      say "Missing system tools:$MISSING. This script does not install Homebrew; once Homebrew is installed (https://brew.sh), the tools are one line:"
      fail "  brew install$MISSING"
    fi
  elif command -v apt >/dev/null 2>&1; then
    say "Missing system tools:$MISSING. Install them with:"
    fail "  sudo apt install$MISSING"
  else
    fail "Missing system tools:$MISSING. Install ffmpeg with your system's package manager, then run this script again."
  fi
fi
[ "$DRY" = 1 ] && [ -z "$MISSING" ] && would "ffprobe already present; nothing to install"

# 3. git: the clone is this route's install, so git is a hard
# prerequisite, named rather than installed.
if ! command -v git >/dev/null 2>&1; then
  fail "git is required and was not found. Install it (macOS: xcode-select --install; Debian/Ubuntu: sudo apt install git), then run this script again."
fi

# The browser interface is built once during source setup.
command -v node >/dev/null 2>&1 && command -v yarn >/dev/null 2>&1 \
  || fail "Install Node 22.12 or newer and Yarn Classic, then rerun setup."

# Reuse a clean main checkout, or clone into a new directory.
if [ -e "$DEST" ]; then
  if [ -d "$DEST/.git" ] && [ "$(git -C "$DEST" remote get-url origin 2>/dev/null || true)" = "$REPO_URL" ]; then
    if [ "$DRY" = 1 ]; then
      would "git -C $DEST pull --ff-only origin $RELEASE_BRANCH (a dirty tree or another branch stops the refresh)"
    else
      [ -z "$(git -C "$DEST" status --porcelain)" ] \
        || fail "$DEST has uncommitted changes. The bootstrap refreshes a clean clone only; commit, stash, or remove them, then run this script again."
      say "Refreshing the existing clone at $DEST to $RELEASE_BRANCH."
      [ "$(git -C "$DEST" branch --show-current)" = "$RELEASE_BRANCH" ] \
        || fail "Switch the checkout to $RELEASE_BRANCH before refreshing."
      git -C "$DEST" pull --ff-only origin "$RELEASE_BRANCH"
    fi
  else
    fail "$DEST already exists and belongs to a different checkout. Choose another LISTENING_INSTALL_DIR."
  fi
else
  if [ "$DRY" = 1 ]; then
      would "clone the selected Git repository"
  else
    say "Cloning the selected repository into $DEST at $RELEASE_BRANCH."
    git clone --branch "$RELEASE_BRANCH" "$REPO_URL" "$DEST"
  fi
fi

set -- setup --client web --non-interactive
[ "$DOWNLOADS" = 0 ] || set -- "$@" --downloads
if [ "$DRY" = 1 ]; then
  would "run ./listening $* in $DEST"
else
  (cd "$DEST" && ./listening "$@")
fi

if [ "$DRY" = 1 ]; then
  say "Dry run complete: nothing was changed."
else
  say "Listening Room $RELEASE_BRANCH is installed at $DEST."
fi
say "Set the owner with listening account set-owner OWNER."
say "Run LISTENING_ROOM_REQUIRE_OWNER=1 listening server start --host 127.0.0.1 and open http://127.0.0.1:8765."
