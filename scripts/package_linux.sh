#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

UV_CACHE_DIR=${UV_CACHE_DIR:-"$root/build/uv-cache"}
PYINSTALLER_CONFIG_DIR=${PYINSTALLER_CONFIG_DIR:-"$root/build/pyinstaller-cache"}
export UV_CACHE_DIR PYINSTALLER_CONFIG_DIR

uv sync --frozen --extra dev
uv run pyinstaller --noconfirm --clean expense_manager_pyqt.spec

stage="$root/build/deb-root"
package="$root/dist/expense-manager_0.2.0_amd64.deb"
rm -rf "$stage"
install -d "$stage/DEBIAN" "$stage/opt/expense-manager" "$stage/usr/bin" "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/256x256/apps"
cp -a "$root/dist/ExpenseManager/." "$stage/opt/expense-manager/"
install -m 0644 "$root/installer/linux/control" "$stage/DEBIAN/control"
install -m 0755 "$root/installer/linux/expense-manager" "$stage/usr/bin/expense-manager"
install -m 0755 "$root/installer/linux/expense-manager-uninstall" "$stage/usr/bin/expense-manager-uninstall"
install -m 0644 "$root/installer/linux/expense-manager.desktop" "$stage/usr/share/applications/expense-manager.desktop"
install -m 0644 "$root/installer/linux/expense-manager-uninstall.desktop" "$stage/usr/share/applications/expense-manager-uninstall.desktop"
install -m 0644 "$root/assets/icons/expense_manager_matte.png" "$stage/usr/share/icons/hicolor/256x256/apps/expense-manager.png"
dpkg-deb --root-owner-group --build "$stage" "$package"
sha256sum "$package" > "$package.sha256"
printf 'Built %s\n' "$package"
