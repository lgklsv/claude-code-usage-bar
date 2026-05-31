# Claude Code Usage Bar

A [SwiftBar](https://github.com/swiftbar/SwiftBar) plugin that shows your **Claude
Code** subscription usage right in the macOS menu bar.

<img width="450" height="416" alt="image" src="https://github.com/user-attachments/assets/f9e8a842-0cea-4dcc-9d93-44aee6d9881a" />


It reports the **same numbers as `/usage` inside the Claude Code CLI** — the real
subscription rate-limit utilization, not a local token estimate. The plugin reads
the OAuth access token that Claude Code stores in your macOS Keychain and calls the
same `https://api.anthropic.com/api/oauth/usage` endpoint that `/usage` uses.

The bar turns **orange at ≥70%** and **red at ≥90%** so you can see at a glance when
you're approaching a limit.

## Requirements

- macOS
- [SwiftBar](https://github.com/swiftbar/SwiftBar) (`brew install swiftbar`) or the
  compatible [xbar](https://github.com/matryer/xbar)
- Python 3 (ships with macOS at `/usr/bin/python3`)
- [Claude Code](https://claude.com/claude-code) installed and **logged in** (so the
  Keychain credentials exist)

## Installation

First, install SwiftBar and open it once so it asks you to pick a **plugin folder**
(e.g. `~/.swiftbar`):

```sh
brew install swiftbar
```

Then clone the repo and **symlink** the plugin into that plugin folder. The symlink
means SwiftBar runs the repo file directly, so the repo stays the single source of
truth and updating is just `git pull`:

```sh
# clone somewhere permanent (not a temp dir)
git clone https://github.com/lgklsv/claude-code-usage-bar.git ~/code/claude-code-usage-bar

# point SwiftBar at it (adjust ~/.swiftbar if your plugin folder differs)
ln -s ~/code/claude-code-usage-bar/claude_usage.5m.py ~/.swiftbar/claude_usage.5m.py
```

Finally, in SwiftBar click the icon → **Refresh All**. The usage bar appears in your
menu bar.

> **Keep the `.5m.py` name** on the symlink — that suffix is what tells SwiftBar to
> refresh every 5 minutes.

Make sure Claude Code is logged in (`claude` → run `/login` if needed) so the plugin
can read your credentials.

## Updating

Pull the repo — that's it. SwiftBar runs the updated file on its next refresh (or
click **Refresh now** in the dropdown to apply it immediately):

```sh
cd ~/code/claude-code-usage-bar && git pull
```

## How it works

- Reads the `Claude Code-credentials` entry from the macOS Keychain via `/usr/bin/security`.
- Calls the OAuth usage endpoint with that token — **no usage is consumed** by polling.
- Caches results to `~/Library/Caches/swiftbar-claude-usage.json` for a short TTL so
  extra SwiftBar re-runs serve the cache instead of hammering the API.
- If the token looks expired, it asks Claude Code's own machinery to refresh
  (`claude auth status`) and retries once — the plugin never performs OAuth itself.
- On a failed fetch it shows the last cached value (marked stale) rather than blanking.

> **Note — this relies on Claude Code internals.** Both the Keychain credential
> (`Claude Code-credentials` and its JSON shape) and the usage endpoint are private to
> Claude Code, not a supported public API. The upside is the plugin rides the same rails
> as `/usage`, so it stays in sync and never exposes your token — but a future Claude
> Code update could change either the credential format or the endpoint. If usage
> suddenly stops appearing, that's the first thing to suspect.

### Why a 5-minute refresh (and not 1 minute)?

The `.5m.py` filename tells SwiftBar to run the plugin every **5 minutes**, not every
minute, and that's on purpose:

- **The usage endpoint rate-limits more strictly than once a minute.** Polling every
  minute would quickly earn an HTTP 429 and leave the bar throttled. A 5-minute cadence
  stays comfortably under the limit. (If a 429 does happen, the plugin honors the
  `Retry-After` header — or backs off `CLAUDE_USAGE_BACKOFF` seconds — and serves the
  cached value meanwhile.)
- **Usage windows are coarse anyway.** The numbers track a 5-hour rolling window and a
  7-day window, so sub-minute freshness buys nothing — the value barely moves between
  one minute and the next.

### Why the cache?

Results are cached to `~/Library/Caches/swiftbar-claude-usage.json` for a TTL of
`295` seconds (`CLAUDE_USAGE_TTL`) — just **under** the 5-minute interval:

- **SwiftBar re-runs the plugin more often than the filename interval** (e.g. on some
  UI/menu events, wake-from-sleep, etc.). Without a cache, each of those extra runs
  would hit the API and risk tripping the rate limit. With it, only the genuinely
  scheduled run refetches; the extra runs serve the cache.
- **TTL sits just below 5 minutes** so the normal scheduled run always finds the cache
  expired and fetches fresh — you still get new numbers every cycle, just not more often
  than the API is happy to serve them.
- The **Refresh now** item in the dropdown deletes the cache first, forcing an immediate
  real fetch when you actually want one.

## Troubleshooting

**The menu bar shows `⚠ Claude` / "usage unavailable".**
Open the dropdown — it shows the underlying error. Most issues are one of the below.

**"no Keychain credentials (is Claude Code logged in?)"**
Claude Code isn't logged in, or the credential has a different name. Open Claude Code
and run `/login`. Verify the Keychain entry exists:

```sh
security find-generic-password -s "Claude Code-credentials" -w
```

The first time the plugin runs, macOS may pop a **Keychain access prompt** — click
**Always Allow** so SwiftBar can read the token without prompting again.

**"token expired — open Claude Code to refresh" (and it stays expired).**
The plugin asks Claude Code to refresh, but it needs the `claude` binary on a path it
can find. SwiftBar runs with a sparse PATH, so set `CLAUDE_BIN` explicitly in the
plugin settings, e.g.:

```sh
CLAUDE_BIN=/opt/homebrew/bin/claude     # Apple Silicon Homebrew
CLAUDE_BIN=$HOME/.local/bin/claude       # native installer
```

Find yours with `which claude`.

**The bar doesn't appear at all.**
- Confirm the file is in the SwiftBar **plugin folder** and is **executable**
  (`chmod +x`).
- Confirm the filename keeps the `.5m.py` refresh-interval suffix.
- Run it directly to see raw output / Python errors:

  ```sh
  /usr/bin/python3 "$HOME/.swiftbar/claude_usage.5m.py"
  ```

**"rate limited — retrying in Ns".**
The usage endpoint throttled the request. The plugin honors `Retry-After` and serves
the cached value meanwhile — just wait; it self-heals.

**Numbers look stale.**
The dropdown notes when it's showing a cached value. Click **Refresh now** in the
dropdown to delete the cache and force a fresh fetch.

## License

[MIT](LICENSE)
