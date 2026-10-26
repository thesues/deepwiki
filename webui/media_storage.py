"""Media backend selection, independent of auth, host and service type."""
import os


def s3_enabled() -> bool:
    value = os.environ.get("MEDIA_USE_S3", "1").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("MEDIA_USE_S3 must be 1 or 0")
