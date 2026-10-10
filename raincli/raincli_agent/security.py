"""Security floors carried by each release (protocol §18.2 item 1).

``MIN_SAFE_VERSION`` is the newest release that contains security fixes.

- **What it bounds:** server-chosen update targets, and any rollback to a version other than the
  one a failed update replaced. Those are refused below it (``below_min_safe``) whatever the
  server sends, in addition to the other version floors.
- **Probation rollback:** going back to the version a failed update replaced is always allowed,
  and is recorded as ``rolled_back_below_min_safe`` when that version is below the floor.
- **Ratchet:** the highest ``MIN_SAFE_VERSION`` this machine has run is recorded (``min-safe.json``
  in the runtime state directory and the install root), and the larger of it and this build's
  value applies, so reinstalling an older release can't lower the floor.
- **Below it:** only a local, explicit ``raincli runtime update --rollback --allow-insecure``,
  with a warning. The app has no such option.

The standalone launcher (``runtime/launcher.py``) carries the same value.
"""
import json
from pathlib import Path

MIN_SAFE_VERSION = "0.5.3"
MIN_SAFE = tuple(int(part) for part in MIN_SAFE_VERSION.split("."))
BELOW_MIN_SAFE = "below_min_safe"
ROLLED_BACK_BELOW = "rolled_back_below_min_safe"
RATCHET_FILE = "min-safe.json"
INSECURE_WARNING = (f"warning: this version is older than v{MIN_SAFE_VERSION}, the oldest release with all "
                    "current security fixes; update again as soon as you can")


def _parse(text):
    try:
        key = tuple(int(part) for part in str(text).removeprefix("v").split("."))
    except ValueError:
        return None
    return key if len(key) == 3 else None


def recorded(directory):
    """The floor recorded in ``directory``, or None."""
    if directory is None:
        return None
    try:
        data = json.loads((Path(directory) / RATCHET_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse(data.get("min_safe")) if isinstance(data, dict) else None


def effective_min(*directories):
    """The larger of this build's ``MIN_SAFE`` and any floor recorded in ``directories``."""
    return max([MIN_SAFE] + [k for k in (recorded(d) for d in directories) if k is not None])


def record(*directories):
    """Ratchet: record the effective floor in each existing directory (never lowers it)."""
    from .fsutil import atomic_write_json
    floor = effective_min(*directories)
    for directory in directories:
        if directory is not None and Path(directory).is_dir() and recorded(directory) != floor:
            try:
                atomic_write_json(Path(directory) / RATCHET_FILE, {"min_safe": "%d.%d.%d" % floor})
            except OSError:
                pass
    return floor


def below_min_safe(version_key, *directories):
    """Whether a version tuple ``(major, minor, patch)`` is below the effective floor."""
    return tuple(version_key) < effective_min(*directories)
