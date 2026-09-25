"""Rollover supervisor: launch | watch | once | status | stop.

The bash behaviour spec is reference_bash/token_supervisor/ccsup; this package
is the port. `once` is a first-class step mode: exactly one bounded pass, then
a clean exit, so the runtime can be driven a tick at a time.
"""
