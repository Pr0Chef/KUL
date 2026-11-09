# A/B run of flat CVA6 synthesis on nangate45, stock side.
# Same settings as the stock nangate45 config.mk (including its 1.0 ns constraint.sdc); the whole CPU
# is flattened into one module before mapping. Results go to nangate45/cva6/flat_stock.
# Choose a stock Yosys (upstream abc_new, one module at a time) for this run, for example with Tools… in the GUI.
include $(dir $(lastword $(MAKEFILE_LIST)))config.mk

export FLOW_VARIANT = flat_stock
export SYNTH_HIERARCHICAL = 0
