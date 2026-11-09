# A/B run of flat CVA6 synthesis, stock side.
# Same settings as config_hier.mk (including the relaxed constraint_hier.sdc) except that the
# whole CPU is flattened into one module before mapping, so the flat and hier A/B runs differ
# only in SYNTH_HIERARCHICAL. Results go to asap7/cva6/flat_stock.
# Choose a stock Yosys (upstream abc_new, one module at a time) for this run, for example with Tools… in the GUI.
include $(dir $(lastword $(MAKEFILE_LIST)))config_hier.mk

export FLOW_VARIANT = flat_stock
export SYNTH_HIERARCHICAL = 0
