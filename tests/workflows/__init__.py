"""End-to-end pilot tests for every settings workflow.

Each test mounts an FNDApp, drives the UI through a complete
workflow (push the menu, navigate to the row, press Enter, click
through confirms, watch the modal complete), and asserts the
expected end state. Catches behavioural regressions that SVG
snapshots miss.

Naming: one file per workflow, named ``test_<verb>_<noun>.py`` so
the failures point straight at the broken workflow.
"""
