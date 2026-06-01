#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# SwiftBar plugin: Claude Code usage in the macOS status bar.
#
# Shows the SAME numbers as `/usage` inside the Claude Code CLI: the real
# subscription rate-limit utilization, not a local token estimate. It reads the
# OAuth access token Claude Code stores in the macOS Keychain and calls
# https://api.anthropic.com/api/oauth/usage (the endpoint /usage itself uses).
#
# Menu bar:  [▮▮▮▯▯▯] 42%   (progress bar; 5-hour rolling session window)
# Dropdown:  5-hour, 7-day, per-model, and extra-usage credit details.
#
# <xbar.title>Claude Code Usage</xbar.title>
# <xbar.desc>5-hour and 7-day Claude subscription usage, same as /usage in the CLI.</xbar.desc>
# <xbar.version>1.0</xbar.version>
#
# Config (optional) via environment in SwiftBar's plugin settings:
#   CLAUDE_USAGE_TTL       cache lifetime in seconds (default 295)
#
# Caching: results are cached to a file for TTL seconds. SwiftBar may re-run the
# plugin more often than the 5-minute filename interval (e.g. on certain UI
# events); within the TTL those extra runs serve the cache instead of calling
# the API. The "Refresh now" item deletes the cache first, forcing a real fetch.
# TTL is set just under the refresh interval so the normal scheduled run always
# refetches. If a fetch fails, the last cached value is shown (marked stale)
# rather than blanking the bar.

import base64
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone

TTL = int(os.environ.get("CLAUDE_USAGE_TTL", "295"))
# When the usage endpoint rate-limits us (HTTP 429) we honor its Retry-After
# header; if it sends none we back off this many seconds. The endpoint's limit
# is stricter than once a minute, so without this the plugin would keep
# refetching every TTL and stay throttled indefinitely.
DEFAULT_BACKOFF = int(os.environ.get("CLAUDE_USAGE_BACKOFF", "600"))

# --- image bar ---
# A rectangular black border with a solid fill inside, drawn as a *template*
# PNG so macOS tints it with the menu-bar label color (black in light mode,
# white in dark) — matching the text. Geometry is per pixel: (W, H) canvas,
# BORDER thickness, PAD_Y transparent margin above/below the box.
INK = (0, 0, 0, 255)  # opaque black; template tinting recolors per appearance

# Both the title and the dropdown bars are cropped TIGHT to content (pad_y=0)
# and pinned to an explicit DISPLAY point size with width=/height=. That keeps
# the image's reported size equal to the visible bar, so neither the dropdown
# row NOR the status-bar hover/selection highlight overgrows — the highlight
# hugs the bar (with menu-bar margin) like the native widgets, instead of
# inheriting the image's tall native pixel height. Each *_BAR is (W, H, BORDER,
# PAD_Y) pixels (drawn at 2× of *_DISPLAY for Retina crispness); *_DISPLAY is
# (width, height) points.
TITLE_BAR = (128, 32, 2, 0)
TITLE_DISPLAY = (64, 16)  # height < menu-bar height, so the highlight has margin
DROP_BAR = (120, 28, 2, 0)
DROP_DISPLAY = (60, 14)

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
MONO = "font=Menlo size=13"


# The Claude Code CLI (self-contained native binary). On an auth/expiry error we
# briefly boot Claude Code's INTERACTIVE session and quit it — Claude Code
# refreshes the OAuth access token (using its long-lived refresh token) during
# startup, before it ever talks to the model, so this costs no usage. We do NOT
# use `claude auth status`: that only READS the cached token and reports on it,
# it never performs the OAuth refresh. The plugin never does OAuth itself.
# Resolve the binary, honoring a user-set CLAUDE_BIN first. SwiftBar runs
# plugins with a sparse GUI PATH (often just /usr/bin:/bin:...), so shutil.which
# alone misses Homebrew on Apple Silicon (/opt/homebrew/bin) and other dirs not
# on that PATH. We therefore also probe the known install locations explicitly:
# native installer (~/.local/bin), Homebrew (arm64 + Intel), and npm-global.
def _find_claude():
    env = os.environ.get("CLAUDE_BIN")
    if env:
        return env
    found = shutil.which("claude")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.local/bin/claude"),  # native installer
        "/opt/homebrew/bin/claude",  # Homebrew (Apple Silicon)
        "/usr/local/bin/claude",  # Homebrew (Intel) / npm
        os.path.expanduser("~/.npm-global/bin/claude"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]  # nothing found; keep a sane default for the error path


CLAUDE_BIN = _find_claude()
# Rows with no action are drawn *disabled* by macOS — dimmed ~50%, which a
# color= can't override (especially weak in light mode). Attaching a harmless
# no-op action makes the row "enabled" so it renders at full label contrast
# (auto-adapting to light/dark). /usr/bin/true does nothing; terminal=false
# keeps it silent; refresh=false so a stray click never calls the API.
ENABLE = "bash=/usr/bin/true terminal=false refresh=false"
CACHE = os.path.join(
    os.path.expanduser("~"), "Library", "Caches", "swiftbar-claude-usage.json"
)


def _png(width, height, rows):
    """Encode RGBA pixel rows (each width*4 bytes) as a PNG — stdlib only."""
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 (none)
        raw.extend(row)

    def chunk(typ, data):
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def bar_image(pct, geom):
    """Base64 PNG progress bar for a (W, H, border, pad_y) pixel geometry:
    rectangular black border, solid fill to the exact fraction, transparent
    track + background. Use with templateImage=."""
    pct = max(0.0, min(100.0, float(pct)))
    W, H, t, pad_y = geom
    x0, x1 = 0, W - 1  # box spans full width (no horizontal margin)
    y0, y1 = pad_y, H - 1 - pad_y  # box outer y (pad_y transparent margin)
    a, b = x0 + t, x1 - t  # inner fillable x
    c, d = y0 + t, y1 - t  # inner fillable y
    fill_px = int(round((b - a + 1) * pct / 100.0))

    rows = [bytearray(W * 4) for _ in range(H)]
    for y in range(y0, y1 + 1):
        row = rows[y]
        for x in range(x0, x1 + 1):
            if x < a or x > b or y < c or y > d:  # border
                color = INK
            elif (x - a) < fill_px:  # solid fill
                color = INK
            else:  # inside-but-unfilled → transparent track
                continue
            i = x * 4
            row[i : i + 4] = bytes(color)
    return base64.b64encode(_png(W, H, rows)).decode("ascii")


def bar_field(pct, sized=False):
    """Return extra_params attaching a template PNG bar that SwiftBar renders as
    the leading icon. Both the dropdown row (sized=True) and the status-bar title
    pin an explicit display size so the image — and thus its hover/selection
    highlight — hugs the bar instead of overgrowing."""
    geom, disp = (DROP_BAR, DROP_DISPLAY) if sized else (TITLE_BAR, TITLE_DISPLAY)
    img = " templateImage=" + bar_image(pct, geom)
    img += " width=%d height=%d" % disp
    return img


def color_for(pct):
    if pct >= 90:
        return "red"
    if pct >= 70:
        return "orange"
    return None


def get_token():
    out = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "Claude Code-credentials",
            "-a",
            os.environ.get("USER", ""),
            "-w",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if out.returncode != 0:
        raise RuntimeError("no Keychain credentials (is Claude Code logged in?)")
    data = json.loads(out.stdout)["claudeAiOauth"]
    expires = data.get("expiresAt")
    if expires and expires / 1000.0 < datetime.now(timezone.utc).timestamp():
        raise RuntimeError("token expired — open Claude Code to refresh")
    return data["accessToken"]


def fetch_usage(token):
    req = urllib.request.Request(
        ENDPOINT,
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_auth_error(e):
    """True if the failure looks like an expired/invalid token (vs. a transient
    network or rate-limit blip that will heal on its own)."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (401, 403)
    msg = str(e).lower()
    return "expired" in msg or "credentials" in msg


def nudge_refresh(timeout=20):
    """Make Claude Code refresh the OAuth token it owns, then quit. We never do
    OAuth ourselves and never send a model request (no usage cost): interactive
    Claude Code refreshes its access token during STARTUP, so we just boot it and
    exit. Interactive mode needs a real TTY, so we run it under a pseudo-terminal
    (a plain subprocess gets no TTY and would never enter interactive mode). Once
    its UI is up we send "/exit"; a hard timeout then SIGKILLs as a backstop.
    Best-effort: every failure is swallowed on purpose."""
    try:
        pid, fd = pty.fork()
    except OSError:
        return
    if pid == 0:  # child: become Claude Code in the PTY
        # SwiftBar runs plugins with a sparse env; interactive Claude wants a
        # TERM and a HOME. Inherit the rest (PATH, USER) from the plugin process.
        os.environ.setdefault("TERM", "xterm-256color")
        os.environ.setdefault("HOME", os.path.expanduser("~"))
        try:
            os.execvp(CLAUDE_BIN, [CLAUDE_BIN])
        except Exception:
            os._exit(127)
    # parent: wait for the UI to come up, then ask it to quit cleanly.
    start = time.time()
    buf = b""
    sent_quit = False
    try:
        while time.time() - start < timeout:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:  # it exited on its own
                return
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:  # EOF: child gone
                    break
                buf += chunk
            # Send "/exit" once the prompt UI has rendered (give it ~1.5s so the
            # input box is ready and doesn't drop the keystrokes).
            if not sent_quit and time.time() - start > 1.5 and b"\x1b[" in buf:
                try:
                    os.write(fd, b"/exit\r")
                except OSError:
                    break
                sent_quit = True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        # Backstop: if it didn't exit from "/exit" in time, kill it. Then reap so
        # we don't leave a zombie behind.
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass


def read_cache():
    """Return (data, fetched_at_epoch, backoff_until_epoch). Missing fields
    default to 0 so an older cache file (no backoff key) still loads."""
    try:
        with open(CACHE) as f:
            blob = json.load(f)
        return (
            blob["data"],
            float(blob["fetched_at"]),
            float(blob.get("backoff_until", 0.0)),
        )
    except Exception:
        return None, 0.0, 0.0


def write_cache(data, fetched_at=None, backoff_until=0.0):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {
                    "fetched_at": time.time() if fetched_at is None else fetched_at,
                    "backoff_until": backoff_until,
                    "data": data,
                },
                f,
            )
        os.replace(tmp, CACHE)  # atomic, avoids a torn read mid-write
    except Exception:
        pass  # caching is best-effort; never fail the render over it


def retry_after_secs(e):
    """Seconds to wait per a 429's Retry-After header, or DEFAULT_BACKOFF.
    Only the integer-seconds form is handled; an HTTP-date falls back."""
    try:
        val = e.headers.get("Retry-After")
        if val is not None:
            return max(1, int(val.strip()))
    except Exception:
        pass
    return DEFAULT_BACKOFF


def reset_str(iso):
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        secs = (when - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return "resetting…"
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        if h >= 24:
            d, h = divmod(h, 24)
            return "resets in %dd %dh" % (d, h)
        return ("resets in %dh %02dm" % (h, m)) if h else ("resets in %dm" % m)
    except Exception:
        return ""


def refresh_item():
    # Delete the cache, THEN refresh — forces a real refetch instead of
    # re-reading the still-fresh cache that a plain refresh=true would hit.
    print(
        'Refresh now | bash=/bin/rm param1=-f param2="%s" terminal=false refresh=true'
        % CACHE
    )


def emit_error(msg):
    print("⚠ Claude")
    print("---")
    print("Claude usage unavailable | color=red")
    print(msg)
    refresh_item()


def line(label, pct, reset):
    c = color_for(pct)
    csuffix = (" color=%s" % c) if c else ""
    img = bar_field(pct, sized=True)
    txt = "%-9s %3.0f%%" % (label, pct)
    if reset:
        txt += "  · " + reset
    print(txt + " | " + MONO + img + " " + ENABLE + csuffix)


def main():
    cached, fetched_at, backoff_until = read_cache()
    now = time.time()
    fresh = cached is not None and (now - fetched_at) < TTL

    if fresh:
        render(cached, stale=False, auth=False)  # cache fresh — no API call
        return

    # Rate-limited recently: don't call the endpoint until the backoff expires,
    # or we just re-trip the limit. Serve the last value (marked stale).
    if now < backoff_until and cached is not None:
        render(cached, stale=True, auth=False)
        return

    try:
        data = fetch_usage(get_token())
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
            wait = retry_after_secs(e)
            # Preserve the original fetched_at so staleness stays honest; only
            # arm the backoff window so the next runs serve cache, not the API.
            write_cache(cached, fetched_at=fetched_at, backoff_until=now + wait)
            if cached is not None:
                render(cached, stale=True, auth=False)
            else:
                emit_error("rate limited — retrying in %ds" % wait)
            return
        if is_auth_error(e):
            # Token likely expired. Don't refresh it ourselves — ask Claude Code
            # to, then retry the fetch ONCE.
            nudge_refresh()
            try:
                data = fetch_usage(get_token())
            except Exception as e2:
                auth = is_auth_error(e2)  # still bad after the nudge
                if cached is not None:
                    render(cached, stale=True, auth=auth)
                else:
                    emit_error(str(e2))
                return
        elif cached is not None:
            render(cached, stale=True, auth=False)  # transient blip — self-heals
            return
        else:
            emit_error(str(e))  # nothing cached and no fetch — show error
            return

    write_cache(data)
    render(data, stale=False, auth=False)


def render(data, stale, auth):
    fh = data.get("five_hour") or {}
    pct = float(fh.get("utilization") or 0.0)

    # --- menu bar title ---
    # The bar is a template PNG; the "% N" text rides alongside it. On an
    # auth-expiry we still show the cached bar, plus a short ⚠ marker (the "open
    # Claude" cue). The template bar can't carry a warning color, so the % text
    # keeps the red/orange cue.
    c = color_for(pct)
    # The bar renders as a PNG, so the title text is just "% N" — let it use the
    # default menu-bar font/size to match the other status items.
    params = bar_field(pct)
    if c:
        params += " color=%s" % c
    marker = " ⚠" if auth else ""
    print("%.0f%%%s | %s" % (pct, marker, params))

    # --- dropdown ---
    print("---")
    print("Claude Code usage | " + ENABLE)
    print("---")
    line("5-hour", pct, reset_str(fh.get("resets_at")))

    sd = data.get("seven_day") or {}
    if sd:
        sp = float(sd.get("utilization") or 0.0)
        line("7-day", sp, reset_str(sd.get("resets_at")))

    for key, label in (
        ("seven_day_opus", "7d Opus"),
        ("seven_day_sonnet", "7d Sonnet"),
    ):
        blk = data.get(key)
        if blk and blk.get("utilization") is not None:
            bp = float(blk["utilization"])
            line(label, bp, reset_str(blk.get("resets_at")))

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled"):
        # used_credits / monthly_limit are in cents (e.g. 10000 == $100.00).
        used = (extra.get("used_credits") or 0.0) / 100.0
        limit = (extra.get("monthly_limit") or 0.0) / 100.0
        cur = extra.get("currency", "USD")
        ep = float(extra.get("utilization") or 0.0)
        print("---")
        line("extra", ep, "")
        print("  $%.2f / $%.2f %s used this month | %s" % (used, limit, cur, ENABLE))

    print("---")
    if auth:
        print("⚠ Open Claude Code to refresh | color=orange")
        print("  (showing last cached value) | " + ENABLE)
    elif stale:
        print("⚠ showing last cached value (refetch failed) | color=orange")
    refresh_item()


if __name__ == "__main__":
    main()
