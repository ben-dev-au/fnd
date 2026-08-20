"""App-level progress strip + session API.

Split across:

* ``model`` — ``Phase`` / ``OperationPlan`` / ``ProgressModel``: what an
  operation looks like and how its phases become one fraction. Pure.
* ``calibration`` — what each phase actually cost, per machine.
* ``bar`` — the widget that paints the strip.
* ``facility`` — ``ProgressFacility`` / ``ProgressSession``, the API callers
  use, plus the visibility policy and the arbitration between classes of work.
* ``operations`` — the concrete plans and the trackers that drive them. This
  is the only module that knows about any particular subsystem.
"""

from __future__ import annotations

from fnd.tui.progress.bar import FNDProgressBar
from fnd.tui.progress.facility import ProgressFacility, ProgressSession

__all__ = ["FNDProgressBar", "ProgressFacility", "ProgressSession"]
