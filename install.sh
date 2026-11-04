#!/usr/bin/env bash
# Mbox Control installer / updater.  Run:  bash install.sh
# Works on Arch (and CachyOS, Manjaro, EndeavourOS), Debian 12+ / Ubuntu 24.04+ (and Mint 22, Pop!_OS),
# and Fedora 39+. Safe to run again: it only adds what's missing and replaces the app file.
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/mbox_control.py"
DEST="$HOME/mbox-control"
WORK="$HOME/.cache/mbox-control-build"
SERVICE_SRC="$HOME/snd-firewire-ctl-services"
say()  { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
ask()  { local a; read -r -p "$1 [Y/n] " a </dev/tty || a=y; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

[ -f "$SRC" ] || { echo "Put install.sh next to mbox_control.py and run it again."; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "Run this as yourself, not root — it asks for your password when needed."; exit 1; }

# ------------------------------------------------------------------ which family of Linux?
. /etc/os-release
FAMILY=""
for id in $ID ${ID_LIKE:-}; do
    case "$id" in
        arch) FAMILY=arch; break ;;
        debian|ubuntu) FAMILY=debian; break ;;
        fedora|rhel|centos) FAMILY=fedora; break ;;
    esac
done
if [ -z "$FAMILY" ]; then
    echo "Sorry, $PRETTY_NAME isn't supported by this installer yet."
    echo "You need: Python 3 + PySide6 + NumPy, FFmpeg, alsa-utils, pactl, PipeWire, and"
    echo "snd-dice-ctl-service from https://github.com/alsa-project/snd-firewire-ctl-services"
    exit 1
fi
echo "Installing on $PRETTY_NAME ($FAMILY family)."

have_pkg() {   # is this package name available in the repositories?
    case "$FAMILY" in
        arch)   pacman -Si "$1" >/dev/null 2>&1 ;;
        debian) apt-cache show "$1" >/dev/null 2>&1 ;;
        fedora) dnf -q info "$1" >/dev/null 2>&1 ;;
    esac
}
install_pkgs() {
    [ $# -gt 0 ] || return 0
    case "$FAMILY" in
        arch)   sudo pacman -S --needed --noconfirm "$@" ;;
        debian) sudo apt-get install -y "$@" ;;
        fedora) sudo dnf install -y "$@" ;;
    esac
}

# ------------------------------------------------------------------ packages
say "Packages (asks for your password)"
PKGS=() ; BUILD=()
case "$FAMILY" in
    arch)
        PKGS=(python pyside6 python-numpy ffmpeg alsa-utils libpulse pipewire pipewire-pulse wireplumber polkit)
        BUILD=(git base-devel meson ninja pkgconf glib2 gobject-introspection alsa-lib systemd-libs curl) ;;
    debian)
        sudo apt-get update
        PKGS=(python3 python3-numpy ffmpeg alsa-utils pulseaudio-utils pipewire-bin wireplumber)
        if have_pkg pkexec; then PKGS+=(pkexec); else PKGS+=(policykit-1); fi
        if have_pkg python3-pyside6.qtwidgets; then
            PKGS+=(python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets
                   python3-pyside6.qtnetwork python3-pyside6.qtsvg)
        else
            PKGS+=(python3-venv python3-pip libxcb-cursor0)   # PySide6 comes from pip below
        fi
        BUILD=(git build-essential meson ninja-build pkg-config libglib2.0-dev gobject-introspection
               libgirepository1.0-dev libasound2-dev libudev-dev python3-setuptools curl)
        have_pkg libhinawa-dev && BUILD+=(libhinawa-dev)
        have_pkg libhitaki-dev && BUILD+=(libhitaki-dev) ;;
    fedora)
        PKGS=(python3 python3-pyside6 python3-numpy alsa-utils pulseaudio-utils pipewire-utils wireplumber polkit)
        command -v ffmpeg >/dev/null || PKGS+=(ffmpeg-free)   # don't fight RPM Fusion's full ffmpeg
        BUILD=(git gcc meson ninja-build pkgconf-pkg-config glib2-devel gobject-introspection-devel
               alsa-lib-devel systemd-devel curl) ;;
esac
install_pkgs "${PKGS[@]}"

# ------------------------------------------------------------------ Python with PySide6
PY=python3
mkdir -p "$DEST"
if ! python3 -c "import PySide6.QtWidgets, PySide6.QtNetwork, PySide6.QtSvg" 2>/dev/null; then
    say "PySide6 isn't packaged here — installing it privately for the app (pip)"
    [ -x "$DEST/venv/bin/python3" ] || python3 -m venv --system-site-packages "$DEST/venv"
    "$DEST/venv/bin/pip" install --upgrade --quiet pip
    "$DEST/venv/bin/pip" install --quiet "PySide6>=6.5" numpy
    PY="$DEST/venv/bin/python3"
fi

# ------------------------------------------------------------------ the Mbox control service
find_service() {
    for p in "$(command -v snd-dice-ctl-service 2>/dev/null || true)" \
             "$SERVICE_SRC/target/release/snd-dice-ctl-service" "/usr/local/bin/snd-dice-ctl-service" \
             "$HOME/.cargo/bin/snd-dice-ctl-service"; do
        [ -n "$p" ] && [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

build_lib() {   # build_lib <pkg-config name> <github repo>
    local pc="$1" repo="$2"
    pkg-config --exists "$pc" && return 0
    echo "Building $repo (not packaged for this system)…"
    rm -rf "${WORK:?}/$repo"
    git clone --quiet --depth 1 "https://github.com/alsa-project/$repo.git" "$WORK/$repo"
    (cd "$WORK/$repo" && meson setup build --prefix=/usr/local -Ddoc=false >/dev/null &&
        meson compile -C build >/dev/null && sudo meson install -C build >/dev/null)
    # make sure the loader and pkg-config can see /usr/local libraries on every distro
    local so; so="$(find /usr/local/lib* -name "lib${pc}.so*" -print -quit 2>/dev/null || true)"
    if [ -n "$so" ]; then
        echo "$(dirname "$so")" | sudo tee /etc/ld.so.conf.d/mbox-control-local.conf >/dev/null
        sudo ldconfig
    fi
}

cargo_ok() {   # recent crates need a recent Rust (1.78+)
    command -v cargo >/dev/null || return 1
    local v; v="$(cargo --version | awk '{print $2}')"
    [ "$(printf '%s\n1.78.0\n' "$v" | sort -V | head -1)" = "1.78.0" ]
}

if SVC="$(find_service)"; then
    echo "Found the control service: $SVC"
else
    say "The Mbox control service (snd-dice-ctl-service)"
    echo "It's what lets any app talk to the Mbox's mixer. It isn't packaged on most systems,"
    echo "so it's built from source: about 5–10 minutes, once."
    if ask "Build it now?"; then
        install_pkgs "${BUILD[@]}"
        mkdir -p "$WORK"
        export PKG_CONFIG_PATH="$(ls -d /usr/local/lib*/pkgconfig /usr/local/lib/*/pkgconfig 2>/dev/null | paste -sd:)${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
        build_lib hinawa libhinawa
        build_lib hitaki libhitaki
        build_lib alsactl alsa-gobject
        export PKG_CONFIG_PATH="$(ls -d /usr/local/lib*/pkgconfig /usr/local/lib/*/pkgconfig 2>/dev/null | paste -sd:)${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
        [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
        if ! cargo_ok; then
            echo "This system's Rust is missing or too old; installing the official one (rustup) in your home folder."
            curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
            . "$HOME/.cargo/env"
        fi
        if [ -d "$SERVICE_SRC/.git" ]; then git -C "$SERVICE_SRC" pull --quiet
        else git clone --quiet https://github.com/alsa-project/snd-firewire-ctl-services.git "$SERVICE_SRC"; fi
        (cd "$SERVICE_SRC" && cargo build --release -p snd-dice-ctl-service)
        SVC="$SERVICE_SRC/target/release/snd-dice-ctl-service"
        echo "Built: $SVC"
    else
        warn "Skipped. The app will open but can't control the Mbox until the service is installed."
    fi
fi

# ------------------------------------------------------------------ sound system
if ! pactl info 2>/dev/null | grep -q "PipeWire"; then
    warn "Your sound system isn't PipeWire (or isn't running right now). Mbox Control needs PipeWire —"
    warn "it's the default on Ubuntu 22.10+, Debian 12, Fedora 34+ and Arch with KDE/GNOME."
fi

# ------------------------------------------------------------------ hardware access
if ! modinfo snd_dice >/dev/null 2>&1; then
    warn "This kernel has no snd-dice driver, so the Mbox can't make sound at all. Most distro kernels include it."
fi
if command -v udevadm >/dev/null &&
        ! grep -rqs "Mbox 3 Pro" /usr/lib/udev/hwdb.d /lib/udev/hwdb.d /etc/udev/hwdb.d; then
    say "Letting you use the Mbox without root (this systemd doesn't know it yet)"
    sudo mkdir -p /etc/udev/rules.d
    echo 'SUBSYSTEM=="firewire", ATTR{vendor}=="0x00a07e", ATTR{model}=="0x000004", TAG+="uaccess"' |
        sudo tee /etc/udev/rules.d/71-mbox-control.rules >/dev/null
    sudo udevadm control --reload || true
    sudo udevadm trigger --subsystem-match=firewire || true
fi

# ------------------------------------------------------------------ the app
say "App"
if [ -f "$DEST/mbox_control.py" ] && ! cmp -s "$SRC" "$DEST/mbox_control.py"; then
    cp "$DEST/mbox_control.py" "$DEST/mbox_control.py.bak"
    echo "Old version kept as $DEST/mbox_control.py.bak"
fi
[ "$SRC" -ef "$DEST/mbox_control.py" ] || cp "$SRC" "$DEST/mbox_control.py"
"$PY" "$DEST/mbox_control.py" --install

# Restart a running copy so the new version takes over
if ("$PY" "$DEST/mbox_control.py" --cmd quit) >/dev/null 2>&1; then
    sleep 1
    nohup "$PY" "$DEST/mbox_control.py" >/dev/null 2>&1 &
    echo "Restarted Mbox Control."
fi
say "Done — open “Mbox Control” from your app launcher"
