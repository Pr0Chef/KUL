# A/B run of flat CVA6 synthesis, parallel side.
# Same settings as config_hier.mk (including the relaxed constraint_hier.sdc) except that the
# whole CPU is flattened into one module before mapping, so the flat and hier A/B runs differ
# only in SYNTH_HIERARCHICAL. Results go to asap7/cva6/flat_parallel.
# Choose the Yosys built from the yosys/ submodule (yosys/build/yosys) for this run, for example with Tools… in the GUI.
include $(dir $(lastword $(MAKEFILE_LIST)))config_hier.mk

export FLOW_VARIANT = flat_parallel
export SYNTH_HIERARCHICAL = 0
