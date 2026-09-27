#!/usr/bin/env python3
"""Persist an H3 input in Autumn S3, then call vLLM-Omni with bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


DEFAULT_S3 = "http://autumn-s3.autumn.svc:9100"
DEFAULT_VLLM = "http://vllm-omni-h3.autumn.svc:8000"
MAX_INPUT_BYTES = 32 << 20
SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}


def safe_session(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", value.strip())
    if not value or value in {".", ".."}:
        raise ValueError("session id is empty or unsafe")
    return value[:128]


def object_url(endpoint: str, key: str) -> str:
    return endpoint.rstrip("/") + "/" + urllib.parse.quote(key, safe="/")


def request_bytes(req: urllib.request.Request, timeout: int) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} from {req.full_url}: {detail}") from exc


def persist_input(path: Path, session_id: str, s3_endpoint: str) -> tuple[str, bytes, str]:
    data = path.read_bytes()
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"input must be 1..{MAX_INPUT_BYTES} bytes")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime not in SUPPORTED_IMAGE_TYPES:
        raise ValueError(f"unsupported image type: {mime}")
    suffix = (path.suffix or ".bin").lower()
    key = f"input/webui/{safe_session(session_id)}/{uuid.uuid4().hex}{suffix}"
    req = urllib.request.Request(
        object_url(s3_endpoint, key), data=data, method="PUT", headers={"Content-Type": mime}
    )
    request_bytes(req, 120)
    return key, data, mime


def load_input(key: str, s3_endpoint: str) -> tuple[bytes, str]:
    if not key.startswith("input/webui/") or ".." in key.split("/"):
        raise ValueError("input object key must be under input/webui/")
    req = urllib.request.Request(object_url(s3_endpoint, key), method="GET")
    data = request_bytes(req, 120)
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"stored input must be 1..{MAX_INPUT_BYTES} bytes")
    mime = mimetypes.guess_type(key)[0] or "application/octet-stream"
    if mime not in SUPPORTED_IMAGE_TYPES:
        raise ValueError(f"unsupported stored image type: {mime}")
    return data, mime


def multipart(fields: dict[str, str], image: tuple[str, bytes, str] | None) -> tuple[bytes, str]:
    boundary = "----buda-h3-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ])
    if image:
        filename, data, mime = image
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="input_reference"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            data,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", type=Path)
    source.add_argument("--input-object-key")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=576)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1101)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    if args.width % 32 or args.height % 32:
        parser.error("width and height must be multiples of 32")
    if not 4 <= args.duration <= 15:
        parser.error("duration must be between 4 and 15 seconds")

    metadata = args.output.with_suffix(args.output.suffix + ".json")
    partial = args.output.with_suffix(args.output.suffix + ".part")
    for candidate in (args.output, metadata, partial):
        if candidate.exists():
            parser.error(
                f"refusing to overwrite existing artifact {candidate}; "
                "choose a new UUID output directory"
            )

    s3_endpoint = os.environ.get("AUTUMN_S3_ENDPOINT", DEFAULT_S3)
    vllm_endpoint = os.environ.get("VLLM_OMNI_URL", DEFAULT_VLLM)
    object_key = None
    image = None
    if args.input:
        object_key, _, _ = persist_input(args.input, args.session_id, s3_endpoint)
    elif args.input_object_key:
        object_key = args.input_object_key
    if object_key:
        image_data, mime = load_input(object_key, s3_endpoint)
        image = (Path(object_key).name, image_data, mime)

    task = "fl2va" if image else "t2va"
    fields = {
        "prompt": args.prompt,
        "width": str(args.width),
        "height": str(args.height),
        "aspect_ratio": "16:9",
        "fps": "24",
        "num_inference_steps": str(args.steps),
        "flow_shift": "12",
        "seed": str(args.seed),
        "extra_params": json.dumps(
            {"task": task, "duration": args.duration, "audio_flow_shift": 3.0},
            separators=(",", ":"),
        ),
    }
    body, content_type = multipart(fields, image)
    req = urllib.request.Request(
        vllm_endpoint.rstrip("/") + "/v1/videos/sync",
        data=body,
        method="POST",
        headers={"Content-Type": content_type},
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as response, partial.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"vLLM HTTP {exc.code}: {detail}") from exc
    elapsed = time.monotonic() - started
    header = partial.read_bytes()[:64]
    if b"ftyp" not in header:
        detail = partial.read_bytes()[:4096].decode("utf-8", "replace")
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"vLLM response is not MP4: {detail}")
    # Link then unlink is an atomic no-clobber publish on the artifact PVC:
    # unlike Path.replace(), it fails if another run created this URL.
    os.link(partial, args.output)
    partial.unlink()

    record = {
        "backend": "vllm-omni-h3",
        "task": task,
        "input_object_key": object_key,
        "output": str(args.output),
        "output_bytes": args.output.stat().st_size,
        "output_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "elapsed_s": round(elapsed, 3),
        "request": {
            "prompt": args.prompt,
            "width": args.width,
            "height": args.height,
            "duration": args.duration,
            "fps": 24,
            "steps": args.steps,
            "seed": args.seed,
            "flow_shift": 12,
            "audio_flow_shift": 3.0,
            "turbo": False,
        },
    }
    with metadata.open("x") as out:
        out.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
