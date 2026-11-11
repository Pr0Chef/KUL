#!/usr/bin/env bash
# Run the GUI tests off screen: each test_gui_*.py drives the app on a private Broadway display, so no window
# or dialog appears on the desktop. Usage: gui/tests/run_gui_tests.sh [test file ...]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPLAY_NO=${BROADWAY_DISPLAY_NO:-7}
broadwayd ":$DISPLAY_NO" >/dev/null 2>&1 &
BROADWAY=$!
trap 'kill $BROADWAY 2>/dev/null' EXIT
sleep 1
export GDK_BACKEND=broadway BROADWAY_DISPLAY=":$DISPLAY_NO"
tests=("$@")
[ ${#tests[@]} -gt 0 ] || tests=("$HERE"/test_gui_*.py)
status=0
for t in "${tests[@]}"; do
	echo "=== $(basename "$t")"
	python3 "$t" || status=1
done
exit $status
