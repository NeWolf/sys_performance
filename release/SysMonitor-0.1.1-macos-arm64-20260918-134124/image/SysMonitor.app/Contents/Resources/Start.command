#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$HOME/Library/Android/sdk/platform-tools:/opt/homebrew/bin:/usr/local/bin:$PATH"
printf '%s\n' 'SysMonitor: Control-C stops the local server, not Android collection.'
exec "$HERE/server/SysMonitor" --open-browser
