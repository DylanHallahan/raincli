"""Security floors carried by each release (protocol §18.2 item 1).

``MIN_SAFE_VERSION`` is the newest release that contains security fixes. Pushed update targets
and automatic rollbacks below it are refused (``below_min_safe``) whatever the server sends,
in addition to the other version floors. Only a local, explicit
``raincli runtime update --rollback --allow-insecure`` goes below it, with a warning; the app
has no such option. The standalone launcher (``runtime/launcher.py``) carries the same value.
"""

MIN_SAFE_VERSION = "0.5.3"
MIN_SAFE = tuple(int(part) for part in MIN_SAFE_VERSION.split("."))
BELOW_MIN_SAFE = "below_min_safe"
INSECURE_WARNING = (f"warning: this version is older than v{MIN_SAFE_VERSION}, the oldest release with all "
                    "current security fixes; update again as soon as you can")


def below_min_safe(version_key):
    """Whether a version tuple ``(major, minor, patch)`` is below ``MIN_SAFE_VERSION``."""
    return tuple(version_key) < MIN_SAFE
