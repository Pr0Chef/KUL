# A/B run of flat CVA6 synthesis on nangate45, parallel side.
# Same settings as the stock nangate45 config.mk (including its 1.0 ns constraint.sdc); the whole CPU
# is flattened into one module before mapping. Results go to nangate45/cva6/flat_parallel.
# Choose the Yosys built from the yosys/ submodule (yosys/build/yosys) for this run, for example with Tools… in the GUI.
include $(dir $(lastword $(MAKEFILE_LIST)))config.mk

export FLOW_VARIANT = flat_parallel
export SYNTH_HIERARCHICAL = 0
