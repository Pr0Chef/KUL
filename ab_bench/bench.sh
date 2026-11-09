#!/usr/bin/env bash
# A/B/B/A CVA6 synthesis from scratch in the installed app, with a warm file cache before every run.
# Usage: [PLATFORM=nangate45] [SPECS="stock parallel"] [N0=run number to start after] bench.sh [hier|flat]
# (default asap7, hier, A/B/B/A from run 1; flat runs also save GUI screenshots)
set -uo pipefail
MODE=${1:-hier}
P=${PLATFORM:-asap7}
S="$(cd "$(dirname "$0")" && pwd)"
ROOT=$(dirname "$S")
# ORFS flow (full clone if present, else orfs/flow) and the two Yosys builds (stock, yosys/ submodule);
# override with ORFS_FLOW, STOCK_YOSYS, PAR_YOSYS.
F=${ORFS_FLOW:-$ROOT/OpenROAD-flow-scripts/flow}
[ -d "$F" ] || F=$ROOT/orfs/flow   # no full clone: use the vendored flow
STOCK=${STOCK_YOSYS:-/usr/local/bin/yosys}
PAR=${PAR_YOSYS:-$ROOT/yosys/build/yosys}
if [ "$MODE" = hier ]; then OUT=$S/bench; NOSHOTS=1; else OUT=$S/bench_$MODE; NOSHOTS=; fi
[ "$P" = asap7 ] || OUT=${OUT}_$P
mkdir -p "$OUT"
warm() {
	find "$F/designs/src/cva6" "$F/designs/$P/cva6" "$F/platforms/$P" "$F/scripts" -type f -exec cat {} + >/dev/null 2>&1
	cat "$STOCK" /usr/local/bin/yosys-abc "$PAR" "$(dirname "$PAR")/yosys-abc" "$(command -v openroad)" >/dev/null
}
n=${N0:-0}
for spec in ${SPECS:-stock parallel parallel stock}; do
	n=$((n + 1))
	if [ "$spec" = stock ]; then Y=$STOCK; else Y=$PAR; fi
	V=${MODE}_$spec
	rm -rf "$F"/{results,logs,reports,objects}/$P/cva6/$V
	warm; sync; sleep 30
	echo "== run $n $spec $(date +%H:%M:%S) load $(cut -d' ' -f1-3 /proc/loadavg)"
	DRV_PLATFORM=$P NOSHOTS=$NOSHOTS GDK_BACKEND=x11 python3 "$S/drive_ab.py" "run${n}_$spec" "config_${MODE}_$spec.mk" "$Y" "$OUT" synth 2>&1 \
		| grep -E "run started|run finished|saved" || true
	R=$OUT/run${n}_$spec; mkdir -p "$R"
	cp "$F"/logs/$P/cva6/$V/*.log "$F"/logs/$P/cva6/$V/*.json "$R"/ 2>/dev/null
	cp "$F"/reports/$P/cva6/$V/synth_stat.txt "$R"/ 2>/dev/null
	sha1sum "$F"/results/$P/cva6/$V/1_2_yosys.v > "$R/netlist.sha1"
	tail -n +2 "$F"/results/$P/cva6/$V/1_2_yosys.v | sha1sum > "$R/netlist_body.sha1"
done
echo "== all done $(date +%H:%M:%S)"
