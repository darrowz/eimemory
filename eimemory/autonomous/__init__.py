"""Experimental autonomous-learning utilities.

The production self-improvement path lives under
``eimemory.governance.autonomous_learning`` and
``eimemory.governance.autonomous_evolution``. This package keeps the
reusable Karpathy-loop utilities that still have value for isolated
experiments: hard time boxes, experiment logs, hypothesis clustering,
compounding context, business feedback, and seven-day review helpers.

It is intentionally not a separate production scheduler. Nightly and
continuous production loops must enter through ``eimemory nightly`` or
``eimemory learn ...`` so there is one state owner for learning goals,
promotion gates, replay evidence, and rollback metadata.
"""
from __future__ import annotations
