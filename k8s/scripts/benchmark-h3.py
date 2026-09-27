#!/usr/bin/env python3
"""Sequential, parameter-matched H3 smoke/latency comparison.

The first request is a warmup; only later requests are reported as hot.  The
script copies the Comfy workflow in memory and never modifies the checked-in
asset (which may contain local user edits).
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import subprocess
import time
import urllib.parse
import urllib.request
import uuid


DEFAULT_PROMPT = (
    "Cinematic shot of a paper boat drifting through a rain-soaked neon street at dusk; "
    "the camera slowly tracks beside it, with rain, distant traffic, and soft wind audible."
)


def json_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def json_post(url: str, value: dict) -> dict:
    data = json.dumps(value).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def encode_multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----buda-benchmark-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(), b"\r\n",
        ]
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def run_vllm(base: str, output: Path, config: dict) -> float:
    fields = {
        "prompt": config["prompt"], "width": str(config["width"]),
        "height": str(config["height"]), "aspect_ratio": "16:9", "fps": "24",
        "num_inference_steps": str(config["steps"]), "flow_shift": "12",
        "seed": str(config["seed"]),
        "extra_params": json.dumps({"task": "t2va", "duration": config["duration"],
                                     "audio_flow_shift": 3.0}, separators=(",", ":")),
    }
    body, content_type = encode_multipart(fields)
    request = urllib.request.Request(
        base.rstrip("/") + "/v1/videos/sync", data=body,
        headers={"Content-Type": content_type}, method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=3600) as response, output.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    return time.monotonic() - started


def comfy_video(result: dict) -> dict:
    for node in result.get("outputs", {}).values():
        for group in ("videos", "gifs", "images"):
            for item in node.get(group, []):
                if str(item.get("filename", "")).lower().endswith(".mp4"):
                    return item
    raise RuntimeError("ComfyUI history contains no MP4 output")


def run_comfy(base: str, template: dict, output: Path, config: dict, run: int) -> float:
    workflow = copy.deepcopy(template)
    workflow["140_131"]["inputs"]["prompt"] = config["prompt"]
    workflow["140_129"]["inputs"]["noise_seed"] = config["seed"]
    workflow["140_133"]["inputs"]["value"] = config["duration"]
    workflow["140_139"]["inputs"]["value"] = False
    workflow["140_137"]["inputs"]["value"] = config["steps"]
    workflow["115"]["inputs"]["aspect_ratio"] = "16:9 (Widescreen)"
    # This selector's 16:9 low-memory setting renders 1056x576 in the deployed
    # ComfyUI build.  Record and control its native megapixel input explicitly;
    # vLLM receives that observed pixel shape, so the compared outputs match.
    workflow["115"]["inputs"]["megapixels"] = config["comfy_megapixels"]
    workflow["92"]["inputs"]["filename_prefix"] = f"benchmark/h3-base-{run}"
    prompt_id = str(uuid.uuid4())
    started = time.monotonic()
    accepted = json_post(base.rstrip("/") + "/prompt", {"prompt": workflow, "prompt_id": prompt_id})
    if accepted.get("node_errors"):
        raise RuntimeError(json.dumps(accepted["node_errors"], ensure_ascii=False))
    while True:
        history = json_get(base.rstrip("/") + "/history/" + prompt_id)
        if prompt_id in history:
            result = history[prompt_id]
            status = result.get("status", {}).get("status_str")
            if status == "error":
                raise RuntimeError(json.dumps(result.get("status"), ensure_ascii=False))
            if status == "success":
                item = comfy_video(result)
                query = urllib.parse.urlencode({"filename": item["filename"],
                                                "subfolder": item.get("subfolder", ""),
                                                "type": item.get("type", "output")})
                with urllib.request.urlopen(base.rstrip("/") + "/view?" + query, timeout=120) as response:
                    output.write_bytes(response.read())
                return time.monotonic() - started
        time.sleep(5)


def probe(path: Path) -> dict:
    try:
        value = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of", "json", str(path)], text=True, timeout=30)
        return json.loads(value)
    except (FileNotFoundError, subprocess.SubprocessError):
        return {"bytes": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vllm-url", default="http://127.0.0.1:18000")
    parser.add_argument("--comfy-url", default="http://127.0.0.1:18188")
    parser.add_argument("--workflow", type=Path, default=Path(
        "webui/hermes/skills/media/comfyui-media/assets/h3_t2v.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/h3-benchmark"))
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--width", type=int, default=1056)
    parser.add_argument("--height", type=int, default=576)
    parser.add_argument("--comfy-megapixels", type=float, default=0.589823)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1101)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--backend", choices=("both", "vllm", "comfyui"), default="both")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = vars(args) | {"turbo": False}
    config = {k: v for k, v in config.items() if k in {
        "prompt", "width", "height", "comfy_megapixels", "duration", "steps", "seed", "turbo"}}
    template = json.loads(args.workflow.read_text())
    results = []
    for run in range(1, args.runs + 1):
        label = "warmup" if run == 1 else "hot"
        backends = (("vllm-omni", run_vllm), ("comfyui", run_comfy))
        for backend, fn in backends:
            if args.backend != "both" and not backend.startswith(args.backend):
                continue
            output = args.output_dir / f"{backend}-{run}.mp4"
            elapsed = fn(args.vllm_url if backend == "vllm-omni" else args.comfy_url,
                         output, config) if backend == "vllm-omni" else fn(
                             args.comfy_url, template, output, config, run)
            entry = {"backend": backend, "run": run, "class": label,
                     "elapsed_s": round(elapsed, 3), "request": config,
                     "output": str(output), "probe": probe(output)}
            results.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    report = {"method": "sequential; run 1 warms caches, run >=2 is hot",
              "results": results}
    (args.output_dir / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
