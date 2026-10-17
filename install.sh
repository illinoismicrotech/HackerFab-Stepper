#!/bin/sh
# Install or update the HackerFab Stepper app on Linux or macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/illinoismicrotech/HackerFab-Stepper/main/install.sh | sh
#
# Downloads the right file for this computer from the GitHub releases, checks its SHA-256 and
# installs it. Running it again updates the app; settings, logs and snapshots are kept.
#
#   Linux   ~/.local/share/HackerfabStepper/HackerfabStepper, plus a menu entry and the
#           command `hackerfab-stepper` in ~/.local/bin
#   macOS   /Applications/HackerfabStepper.app (~/Applications if /Applications is not writable)
#
# Options (after `sh -s --` when piped):  --release   newest numbered release instead of the
#                                                     Latest build of main
#                                         --dir DIR   install somewhere else
set -eu

REPO="illinoismicrotech/HackerFab-Stepper"
NAME="HackerfabStepper"
CHANNEL="latest"
DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --release) CHANNEL="release" ;;
        --dir) DIR="$2"; shift ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '  %s\n' "$*"; }
fail() { printf '\n  Error: %s\n\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || fail "curl is needed."

case "$(uname -s)" in
    Linux)
        ARCH="$(uname -m)"
        [ "$ARCH" = "amd64" ] && ARCH="x86_64"
        ASSET="$NAME-Linux-$ARCH.tar.gz"
        DIR="${DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/$NAME}"
        ;;
    Darwin)
        if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = "1" ]; then ARCH=arm64; else ARCH=x86_64; fi
        ASSET="$NAME-macOS-$ARCH.zip"
        if [ -z "$DIR" ]; then
            if [ -w /Applications ]; then DIR=/Applications; else DIR="$HOME/Applications"; fi
        fi
        ;;
    *) fail "this script is for Linux and macOS. On Windows use install.ps1." ;;
esac

echo ""
echo "  HackerFab Stepper installer"
echo "  ───────────────────────────"

# ── Find the download ──────────────────────────────────────────────────────
if [ "$CHANNEL" = "latest" ]; then
    API="https://api.github.com/repos/$REPO/releases/tags/latest"
else
    API="https://api.github.com/repos/$REPO/releases/latest"
fi
AUTH=""
[ -n "${GITHUB_TOKEN:-}" ] && AUTH="Authorization: Bearer $GITHUB_TOKEN"
JSON="$(curl -fsSL -H 'Accept: application/vnd.github+json' ${AUTH:+-H "$AUTH"} "$API")" \
    || fail "could not get the release from GitHub ($API). Check the internet connection; with --release, there may be no numbered release yet."
# One JSON field per line (works with the BSD tools on macOS too); the asset's checksum is the
# first "digest" after its name.
FIELDS="$(printf '%s' "$JSON" | tr ',' '\n')"
TAG="$(printf '%s\n' "$FIELDS" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
SHA="$(printf '%s\n' "$FIELDS" | awk -v want="\"$ASSET\"" '
    index($0, "\"name\"") && index($0, want) { found = 1 }
    found && match($0, /sha256:[0-9a-f]+/) { print substr($0, RSTART + 7, RLENGTH - 7); exit }')"
[ -n "$TAG" ] || fail "unexpected answer from GitHub."
[ -n "$SHA" ] || fail "this release has no $ASSET with a checksum (it may still be building; try again in a few minutes)."
URL="https://github.com/$REPO/releases/download/$TAG/$ASSET"

say "Release:  $TAG"
say "File:     $ASSET"
say "Into:     $DIR"
echo ""

# ── Download and verify ────────────────────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fL --progress-bar -o "$TMP/$ASSET" "$URL" || fail "the download failed."
if command -v sha256sum >/dev/null; then
    GOT="$(sha256sum "$TMP/$ASSET" | cut -d' ' -f1)"
else
    GOT="$(shasum -a 256 "$TMP/$ASSET" | cut -d' ' -f1)"
fi
[ "$GOT" = "$SHA" ] || fail "the download is damaged (checksum mismatch). Nothing was changed."
say "Checksum OK"

# ── Install (the previous copy is replaced in one step) ────────────────────
mkdir -p "$DIR"
if [ "$(uname -s)" = "Linux" ]; then
    tar -xzf "$TMP/$ASSET" -C "$TMP" "$NAME"
    chmod 755 "$TMP/$NAME"
    mv -f "$TMP/$NAME" "$DIR/$NAME"
    mkdir -p "$HOME/.local/bin" "${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    ln -sf "$DIR/$NAME" "$HOME/.local/bin/hackerfab-stepper"
    cat > "${XDG_DATA_HOME:-$HOME/.local/share}/applications/hackerfab-stepper.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=HackerFab Stepper
Comment=Photolithography stepper control
Exec=$DIR/$NAME
Terminal=false
Categories=Science;Engineering;
EOF
    say "Installed $DIR/$NAME"
    say "Start it from the app menu (HackerFab Stepper) or with: hackerfab-stepper"
    if ! id -nG | grep -qwE 'video' || ! id -nG | grep -qwE 'dialout|uucp'; then
        echo ""
        say "Once, so the app may use the camera and the stage controller:"
        if grep -qi arch /etc/os-release 2>/dev/null; then GROUP=uucp; else GROUP=dialout; fi
        say "  sudo usermod -aG video,$GROUP \$USER     (then log out and back in)"
    fi
else
    ditto -x -k "$TMP/$ASSET" "$TMP/unpacked"
    rm -rf "$DIR/$NAME.app.old"
    [ -d "$DIR/$NAME.app" ] && mv "$DIR/$NAME.app" "$DIR/$NAME.app.old"
    mv "$TMP/unpacked/$NAME.app" "$DIR/$NAME.app"
    rm -rf "$DIR/$NAME.app.old"
    say "Installed $DIR/$NAME.app"
    say "Start it from Launchpad or Applications (HackerFab Stepper)."
fi
echo ""
say "Updates: the app checks GitHub once a day (Settings → Updates), or run this script again."
echo ""
