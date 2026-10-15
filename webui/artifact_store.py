"""Generated media: local staging, immutable objects in unauthenticated Autumn S3.

The public /artifacts/<session>/<path> URL stays stable. Only completed files
are published; writers must use a .part name and rename after closing it.
No S3 credentials, signed links, or media bodies are stored in chat history.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import urllib.parse

from botocore.exceptions import ClientError

from media_input import endpoint
from s3_client import client, object_args

log = logging.getLogger("deepwiki.artifacts")
CHUNK = 256 * 1024
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}
MEDIA_SUFFIXES = VIDEO_SUFFIXES | {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".svg", ".heic", ".heif"}
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def media_path(relative: str) -> bool:
    parts = relative.split("/")
    return (len(parts) >= 2 and SESSION.fullmatch(parts[0]) is not None
            and all(p and not p.startswith(".") and "\\" not in p and "\x00" not in p for p in parts)
            and PurePosixPath(relative).suffix.lower() in MEDIA_SUFFIXES)


def object_key(relative: str) -> str:
    if not media_path(relative):
        raise ValueError("invalid media artifact path")
    prefix = os.environ.get("ARTIFACT_S3_PREFIX", "output/webui").strip("/")
    if not prefix or any(p in {"", ".", ".."} for p in prefix.split("/")):
        raise ValueError("invalid ARTIFACT_S3_PREFIX")
    return f"{prefix}/{relative}"


def head(relative: str):
    try:
        return client(endpoint()).head_object(**object_args(object_key(relative)))
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            return None
        raise


def open_media(relative: str, **conditions):
    return client(endpoint()).get_object(**object_args(object_key(relative)), **conditions)


def _digest(source, checksum=None) -> str:
    digest = hashlib.sha256()
    while block := source.read(CHUNK):
        digest.update(block)
        if checksum is not None:
            checksum.update(block)
    return digest.hexdigest()


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def publish_file(root: Path, path: Path) -> str:
    """PUT a closed media file; delete staging only after verified persistence.

    A lost PUT response is retryable: conditional PUT never overwrites a URL,
    and a collision is accepted only after comparing the full remote digest.
    """
    root = root.resolve()
    resolved = path.resolve()
    relative = resolved.relative_to(root).as_posix()
    key = object_key(relative)
    if path.is_symlink():
        raise ValueError("cannot publish a symlink")
    with resolved.open("rb") as source:
        before = _signature(os.fstat(source.fileno()))
        if before[2] == 0:
            raise ValueError(f"empty media artifact: {relative}")
        checksum = hashlib.md5()
        digest = _digest(source, checksum)
        if _signature(os.fstat(source.fileno())) != before:
            raise RuntimeError(f"media changed during publication: {relative}")
        source.seek(0)
        try:
            client(endpoint()).put_object(
                **object_args(key), Body=source, ContentLength=before[2],
                ContentType="application/octet-stream", IfNoneMatch="*",
                ContentMD5=base64.b64encode(checksum.digest()).decode("ascii"),
            )
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            with open_media(relative)["Body"] as remote:
                if _digest(remote) != digest:
                    raise ValueError(f"artifact URL already contains different media: {relative}") from exc
        saved = head(relative)
        if saved is None or saved.get("ContentLength") != before[2]:
            raise RuntimeError(f"S3 verification failed: {relative}")
        if _signature(os.fstat(source.fileno())) != before or _signature(resolved.stat()) != before:
            raise RuntimeError(f"media changed during publication: {relative}")
    resolved.unlink()
    return f"/artifacts/{urllib.parse.quote(relative, safe='/')}"


def make_poster(video: Path) -> Path | None:
    """Create the small video.jpg before its source leaves local staging."""
    poster = video.with_suffix(".jpg")
    if poster.is_file():
        return poster
    fd, temporary = tempfile.mkstemp(prefix=".poster-", suffix=".jpg", dir=video.parent)
    os.close(fd)
    candidate = Path(temporary)
    try:
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", "0.1", "-i", str(video), "-frames:v", "1",
            "-vf", "scale=640:-2:force_original_aspect_ratio=decrease",
            "-q:v", "5", "-f", "image2", str(candidate),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=False)
        if result.returncode or not candidate.stat().st_size:
            log.warning("poster generation failed for %s", video)
            return None
        try:
            os.link(candidate, poster)
        except FileExistsError:
            pass
        return poster
    except (OSError, subprocess.SubprocessError):
        log.warning("poster generation failed for %s", video, exc_info=True)
        return None
    finally:
        candidate.unlink(missing_ok=True)


def existing_media(root: Path, session_id: str) -> set[str]:
    """Take a metadata-only baseline so resuming a session leaves old media alone."""
    return {p.relative_to(root).as_posix() for p in (root / session_id).rglob("*")
            if p.is_file() and media_path(p.relative_to(root).as_posix())}


def _pending(path: Path) -> Path:
    return path.with_name(f".{path.name}.s3-pending")


def _publish_marked(root: Path, path: Path) -> str:
    url = publish_file(root, path)
    _pending(path).unlink(missing_ok=True)
    return url


def publish_session(root: Path, session_id: str, *, exclude=()) -> list[str]:
    """Finalize media in one session, retaining failed files for a later retry."""
    if not SESSION.fullmatch(session_id) or session_id in {".", ".."}:
        raise ValueError("invalid artifact session")
    root = root.resolve()
    directory = root / session_id
    if not directory.is_dir():
        return []
    if directory.resolve() != directory:
        raise ValueError("cannot publish a symlink session")
    # The agent can explicitly publish before the lifecycle fallback runs.
    # Serialize those processes without retaining media or a lock table.
    with (directory / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish_directory(root, directory, exclude)


def _publish_directory(root: Path, directory: Path, exclude) -> list[str]:
    published = []
    # Do not follow symlinks or publish hidden temporary files. Images go after
    # videos, since a video publisher also uploads its poster.
    paths = [p for p in directory.rglob("*")
             if p.is_file() and media_path(p.relative_to(root).as_posix())
             and p.resolve() == p.absolute()
             and (p.relative_to(root).as_posix() not in exclude or _pending(p).is_file())]
    # Mark every new output before the first upload. If one fails, the next
    # turn retries this batch while still excluding untouched legacy media.
    for path in paths:
        _pending(path).touch(exist_ok=True)
    for path in sorted(paths, key=lambda p: p.suffix.lower() not in VIDEO_SUFFIXES):
        if not path.is_file():  # a video's poster was already published
            continue
        if path.suffix.lower() in VIDEO_SUFFIXES:
            # Do not regenerate a different JPEG at an already-published URL
            # after a previous video PUT failed.
            poster = path.with_suffix(".jpg")
            if head(poster.relative_to(root).as_posix()) is None:
                poster = make_poster(path)
                if poster is not None:
                    published.append(_publish_marked(root, poster))
        published.append(_publish_marked(root, path))
    return published


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_id", help="publish completed media in this session")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("HERMES_HOME", "/opt/data")) / "artifacts")
    args = parser.parse_args()
    for url in publish_session(args.root, args.session_id):
        print(url)


if __name__ == "__main__":
    main()
