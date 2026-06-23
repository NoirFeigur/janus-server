"""Channel health monitoring and auto-disable.

Tracks per-channel error rates in Redis sliding windows, auto-degrades channels
exceeding thresholds, runs recovery probes, and hard-disables after sustained
probe failures.
"""

from __future__ import annotations
