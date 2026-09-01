"""XIOSYNC control plane.

Import namespace aggregator (DECISIONS.md D-019): the physical layout keeps
``api/ services/ domain/ persistence/ platform/`` at the repository root per
doc 04 §6 / D-015; this package exposes them as ``xiosync.*`` because a
top-level Python package named ``platform`` would collide with the standard
library module of the same name. Subdirectories here are symlinks to the root
directories — the root directories remain the canonical homes.
"""
