#!/usr/bin/env bash
# Build orfs-flow-runner_<version>_all.deb, which installs the app under /opt/orfs-flow-runner with an
# app-menu entry and an orfs-flow-runner command. OpenROAD, Yosys and ORFS itself are not included.
# Usage: gui/packaging/build_deb.sh [output dir, default: dist]
# Install: sudo apt install ./dist/orfs-flow-runner_<version>_all.deb
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUI="$(dirname "$PKG_DIR")"
OUT="$(mkdir -p "${1:-dist}" && cd "${1:-dist}" && pwd)"
NAME=orfs-flow-runner
VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$GUI/orfs_app.py")"
[ -n "$VERSION" ] || { echo "VERSION not found in orfs_app.py" >&2; exit 1; }
MAINTAINER="$(git -C "$GUI" config user.name || echo unknown) <$(git -C "$GUI" config user.email || echo unknown@localhost)>"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOTFS="$STAGE/$NAME"

# orfs_app.py imports the metrics code from flow_gui.py and the Inputs tab from inputs_tab.py, which reads
# clocks with sdc_clocks.py.
for f in orfs_app.py flow_gui.py inputs_tab.py sdc_clocks.py; do
	install -Dm644 "$GUI/$f" "$ROOTFS/opt/$NAME/$f"
done
install -Dm644 "$PKG_DIR/$NAME.desktop" "$ROOTFS/usr/share/applications/$NAME.desktop"
install -Dm644 "$PKG_DIR/$NAME.svg" "$ROOTFS/usr/share/icons/hicolor/scalable/apps/$NAME.svg"
install -d "$ROOTFS/usr/bin"
cat > "$ROOTFS/usr/bin/$NAME" <<EOF
#!/bin/sh
exec python3 /opt/$NAME/orfs_app.py "\$@"
EOF
chmod 755 "$ROOTFS/usr/bin/$NAME"

install -d "$ROOTFS/DEBIAN"
cat > "$ROOTFS/DEBIAN/control" <<EOF
Package: $NAME
Version: $VERSION
Section: electronics
Priority: optional
Architecture: all
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-3.0, make, tcl
Recommends: klayout
Installed-Size: $(du -sk "$ROOTFS" | cut -f1)
Maintainer: $MAINTAINER
Description: Desktop app to run OpenROAD-flow-scripts designs from RTL to GDS
 Pick a platform, design and config from an ORFS flow folder, the CPU cores and the
 stage range, then run. The Inputs tab edits the design's SDC, flow settings and files.
 Reports, logs and debug information have their own tabs.
 OpenROAD, Yosys and an OpenROAD-flow-scripts checkout must be installed separately.
EOF
# Byte-compile at install time so the app does not try to write __pycache__ under /opt.
cat > "$ROOTFS/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e
python3 -m compileall -q /opt/$NAME >/dev/null 2>&1 || true
EOF
cat > "$ROOTFS/DEBIAN/prerm" <<EOF
#!/bin/sh
set -e
rm -rf /opt/$NAME/__pycache__
EOF
chmod 755 "$ROOTFS/DEBIAN/postinst" "$ROOTFS/DEBIAN/prerm"

DEB="$OUT/${NAME}_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$ROOTFS" "$DEB" >/dev/null
echo "built $DEB"
