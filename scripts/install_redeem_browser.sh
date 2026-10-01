#!/usr/bin/env bash
# ============================================================
# install_redeem_browser.sh — set up the ruyiPage browser engine
# ============================================================
# The redeem-code scraper can optionally drive a fingerprinted Firefox
# (ruyiPage) to pierce ucngame's Cloudflare JS challenge, which plain
# HTTP clients cannot pass. This script installs that engine into an
# ISOLATED venv + a managed Firefox runtime, so nothing touches the
# production bot venv (~/.virtualenvs/QQBotAgent) and a browser crash
# can never take down the bot.
#
# After it finishes, enable the engine by adding to QQBot/.env:
#   REDEEM_CODE_BROWSER=ruyipage
#   REDEEM_CODE_BROWSER_PYTHON=$HOME/.virtualenvs/ruyipage/bin/python
#   REDEEM_CODE_BROWSER_TIMEOUT=90
# then restart the bot. xvfb-run must be present (apt install xvfb).
#
# Idempotent: safe to re-run. Override via env:
#   RUYIPAGE_VENV            venv path   (default ~/.virtualenvs/ruyipage)
#   REDEEM_BROWSER_PROXY     http proxy  (default http://127.0.0.1:1081;
#                            set empty for a direct/no-proxy machine)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${RUYIPAGE_VENV:-$HOME/.virtualenvs/ruyipage}"
PROXY="${REDEEM_BROWSER_PROXY-http://127.0.0.1:1081}"   # note: no colon => empty allowed
REPO="LoseNine/ruyipage"
# Single source of truth for the engine's Python deps (keeps them out of the
# bot venv's requirements.txt — see the file header for the isolation rationale).
BROWSER_REQS="${REDEEM_BROWSER_REQS:-$SCRIPT_DIR/requirements-browser.txt}"

BLUE='\033[0;34m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
log()  { printf "${BLUE}[install-browser]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}[install-browser]${NC} %s\n" "$*"; }
err()  { printf "${RED}[install-browser]${NC} %s\n" "$*" >&2; }

# curl proxy flag (empty PROXY => direct)
if [ -n "$PROXY" ]; then PROXY_ARG=(-x "$PROXY"); else PROXY_ARG=(); fi

# ── 0. prerequisites ────────────────────────────────────────────
if ! command -v xvfb-run >/dev/null 2>&1; then
    err "xvfb-run not found. Install it first:  sudo apt-get install -y xvfb"
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found."; exit 1
fi

# ── 1. dedicated venv ───────────────────────────────────────────
if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
else
    log "venv exists at $VENV_DIR"
fi
PY="$VENV_DIR/bin/python"

# ── 2. python packages ──────────────────────────────────────────
if [ ! -f "$BROWSER_REQS" ]; then
    err "requirements file not found: $BROWSER_REQS"; exit 1
fi
log "installing engine deps from $(basename "$BROWSER_REQS") (pip)"
"$PY" -m pip install -q --upgrade pip
# pip usually has a fast local mirror configured; try direct, then proxy.
if ! "$PY" -m pip install -q -r "$BROWSER_REQS" 2>/dev/null; then
    log "direct pip failed; retrying via proxy"
    HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" \
        "$PY" -m pip install -q -r "$BROWSER_REQS"
fi
RUYI_VER="$("$PY" -c 'import ruyipage;print(getattr(ruyipage,"__version__","?"))')"
ok "ruyiPage $RUYI_VER installed in venv"

# ── 3. Firefox runtime ──────────────────────────────────────────
runtime_installed() {
    "$PY" -m ruyipage doctor 2>/dev/null | grep -q "installed: yes"
}

if runtime_installed; then
    ok "Firefox runtime already installed"
else
    log "installing Firefox runtime (native installer, via proxy if set)"
    # The native installer uses urllib, which on some networks hits an SSL
    # handshake timeout against GitHub's release-asset CDN. Try it first
    # (with proxy), then fall back to a manual curl download.
    if HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" "$PY" -m ruyipage install; then
        ok "native install succeeded"
    else
        err "native install failed; falling back to manual download"
        DOCTOR="$("$PY" -m ruyipage doctor 2>/dev/null || true)"
        RELEASE="$(printf '%s\n' "$DOCTOR" | sed -n 's/.*release:[[:space:]]*//p' | tr -d '[:space:]' | head -1)"
        BIN_PATH="$(printf '%s\n' "$DOCTOR" | sed -n 's/.*path:[[:space:]]*//p' | tr -d '[:space:]' | head -1)"
        if [ -z "$RELEASE" ] || [ -z "$BIN_PATH" ]; then
            err "could not read release/path from doctor output:"; printf '%s\n' "$DOCTOR" >&2; exit 1
        fi
        # browser dir = .../firefox-<ver>-<release>-<platform>  (bin is <dir>/firefox/firefox)
        BROWSER_DIR="$(dirname "$(dirname "$BIN_PATH")")"
        log "release=$RELEASE  browser_dir=$BROWSER_DIR"

        log "discovering asset URL via GitHub API"
        API_JSON="$(curl -sSL "${PROXY_ARG[@]}" "https://api.github.com/repos/$REPO/releases/tags/$RELEASE")"
        ASSET_URL="$(printf '%s\n' "$API_JSON" \
            | grep -oE '"browser_download_url":[[:space:]]*"[^"]+"' \
            | sed 's/.*"browser_download_url":[[:space:]]*"//; s/"$//' \
            | grep -E 'linux-x86_64.*\.tar\.xz' | head -1 || true)"
        if [ -z "$ASSET_URL" ]; then
            err "could not find a linux-x86_64 .tar.xz asset for $RELEASE"
            err "download it manually from https://github.com/$REPO/releases/tag/$RELEASE"
            err "and extract so that this binary exists: $BIN_PATH"
            exit 1
        fi
        log "asset: $ASSET_URL"

        TMP_TAR="$(mktemp -t ruyi-firefox-XXXXXX.tar.xz)"
        log "downloading runtime (~80MB) via proxy"
        curl -sSL "${PROXY_ARG[@]}" -o "$TMP_TAR" "$ASSET_URL"
        if ! xz -t "$TMP_TAR" 2>/dev/null; then
            err "downloaded file is not valid xz data"; rm -f "$TMP_TAR"; exit 1
        fi
        mkdir -p "$BROWSER_DIR"
        log "extracting into $BROWSER_DIR"
        tar xJf "$TMP_TAR" -C "$BROWSER_DIR"
        rm -f "$TMP_TAR"
    fi
fi

# ── 4. verify ───────────────────────────────────────────────────
if runtime_installed; then
    ok "Firefox runtime verified:"
    "$PY" -m ruyipage doctor 2>/dev/null | sed 's/^/    /'
else
    err "runtime still not installed after fallback; check network/proxy"
    exit 1
fi

# ── 5. smoke test (optional, off by default to avoid a real fetch) ──
if [ "${1:-}" = "--smoke" ]; then
    log "smoke test: fetching ucngame via xvfb-run (this hits the network)"
    REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
    OUT="$(mktemp -t ruyi-smoke-XXXXXX.html)"
    if xvfb-run -a "$PY" "$REPO_ROOT/QQBot/plugins/ruyipage_fetch.py" \
            "https://ucngame.com/codes/ark-recode-redeem-codes/" "$OUT" 60; then
        ok "smoke test PASSED — $(wc -c < "$OUT") bytes fetched"
    else
        err "smoke test FAILED (exit $?)"
    fi
    rm -f "$OUT"
fi

cat <<EOF

$(printf "${GREEN}ruyiPage browser engine ready.${NC}")

Enable it by adding to QQBot/.env, then restart the bot:

    REDEEM_CODE_BROWSER=ruyipage
    REDEEM_CODE_BROWSER_PYTHON=$VENV_DIR/bin/python
    REDEEM_CODE_BROWSER_TIMEOUT=90

The scraper will then drive Firefox (under xvfb-run) to clear ucngame's
Cloudflare challenge and auto-refresh codes. With the flag unset, behavior
is unchanged (panel manual maintenance stays primary).
EOF
