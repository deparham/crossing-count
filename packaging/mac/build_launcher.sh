#!/bin/sh
# Build the Mac launcher, ~/Applications/CrossingCount.app, from CrossingCount.applescript.
# Drag it to the Dock to open CrossingCount with one click.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/Applications"
rm -rf "$HOME/Applications/CrossingCount.app"
osacompile -o "$HOME/Applications/CrossingCount.app" "$here/CrossingCount.applescript"
echo "Built $HOME/Applications/CrossingCount.app"
