#!/usr/bin/env bash
# Installs the `foundry-manager` command and its application-menu entry for you
# (no sudo). Run again after moving the folder. `./install.sh --remove` undoes it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin/foundry-manager"
desktop="${XDG_DATA_HOME:-$HOME/.local/share}/applications/foundry-manager.desktop"

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$bin" "$desktop"
    echo "Removed $bin and $desktop."
    exit 0
fi

chmod +x "$here/foundry_manager.py"
mkdir -p "$(dirname "$bin")" "$(dirname "$desktop")"
ln -sfn "$here/foundry_manager.py" "$bin"
echo "Command: $bin -> $here/foundry_manager.py"

cat > "$desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Foundry Server Manager
Comment=Run and add Foundry VTT servers
Exec="$here/foundry_manager.py"
Icon=applications-games
Categories=Game;Network;
Terminal=false
DESKTOP
echo "Menu entry: $desktop"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) echo "Done. Run it with: foundry-manager" ;;
    *) echo "Done. ~/.local/bin isn't on your PATH yet, so add this line to ~/.bashrc (or your shell's rc file):"
       echo '    export PATH="$HOME/.local/bin:$PATH"'
       echo "and open a new terminal. Until then: $bin" ;;
esac
