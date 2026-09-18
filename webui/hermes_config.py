"""Point hermes at the MCP server, through its config file.

Why the config file and not ACP `session/new {mcpServers: [...]}`: the config
shape `mcp_servers.<name>.url` is the one hermes' own tests exercise for an
HTTP-transport server, and it applies to EVERY session including ones loaded
from history. The ACP parameter's HTTP form was not verified against this
hermes build, and guessing a wire shape that silently parses to "no servers"
would look exactly like a working deploy with an agent that has no tools —
the failure mode this whole decoupling exists to avoid.

Written without pyyaml on purpose: it is not guaranteed present in the runtime,
and a missing import here would degrade into "chat works, retrieval silently
does not". hermes writes a plain block-style mapping, so an indentation-aware
edit is enough and dependency-free. The same reasoning the console applied to
reading the `model:` block.

Toolsets live in the SAME file, under `platform_toolsets.cli` — the key
hermes' own CLI reads for itself (see `resolve_toolsets` below). One file owns
what a session can do; the env vars that used to drive this directly are
seeds, not sources.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("webui.config")


# The toolsets a session runs with when the config file says nothing. Terminal
# is in it on purpose: this UI exists so a skill can run something, and a
# config that predates the key must not silently produce an agent that cannot.
# (This is also the list the deployment had before the config file owned the
# knob, carried over unchanged.)
DEFAULT_TOOLSETS = [
    "file", "terminal", "todo", "memory", "skills",
    "search", "web", "session_search", "clarify",
]


def render_block(name: str, url: str) -> list[str]:
    return [
        "mcp_servers:",
        f"  {name}:",
        f"    url: {url}",
        "    enabled: true",
    ]


def ensure_mcp_server(config_path: Path, name: str, url: str) -> bool:
    """Make `mcp_servers.<name>.url` say `url`. Returns True if the file changed.

    Idempotent: an unchanged desired state rewrites nothing, so a restart loop
    does not churn the file hermes may be reading.
    """
    lines = config_path.read_text().splitlines() if config_path.exists() else []

    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "mcp_servers:"), None)
    if start is None:
        new = [*lines, *([""] if lines and lines[-1].strip() else []), *render_block(name, url)]
        _write(config_path, new)
        log.info("hermes config: added mcp_servers.%s -> %s", name, url)
        return True

    # The block runs to the next line at column 0 that is not blank — the same
    # indentation rule hermes' own writer produces.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[0].isspace():
            end = i
            break
    block = lines[start:end]

    want = f"    url: {url}"
    entry = next((i for i, ln in enumerate(block) if ln.strip() == f"{name}:"), None)
    if entry is None:
        block = [*block, f"  {name}:", want, "    enabled: true"]
    else:
        # Replace this server's url line; leave every sibling key alone so an
        # operator's headers/timeout settings survive.
        stop = len(block)
        for i in range(entry + 1, len(block)):
            if block[i].strip() and not block[i].startswith("    "):
                stop = i
                break
        sub = block[entry + 1 : stop]
        url_at = next((i for i, ln in enumerate(sub) if ln.strip().startswith("url:")), None)
        if url_at is None:
            sub = [want, *sub]
        elif sub[url_at] == want:
            return False  # already correct — do not touch the file
        else:
            sub[url_at] = want
        block = [*block[: entry + 1], *sub, *block[stop:]]

    _write(config_path, [*lines[:start], *block, *lines[end:]])
    log.info("hermes config: set mcp_servers.%s -> %s", name, url)
    return True


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: hermes may read this file at any moment, and a
    # half-written config parses as a config with no MCP servers.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n")
    tmp.replace(path)


# ── toolsets ────────────────────────────────────────────────────────────────


def render_toolsets_block(toolsets: list[str]) -> list[str]:
    # Block style, one entry per line — hermes' own writer produces the same
    # shape, and a line-scan can tell "configured" from "absent" at a glance.
    return ["platform_toolsets:", "  cli:", *(f"    - {t}" for t in toolsets)]


def ensure_platform_toolsets(config_path: Path, toolsets: list[str] | None = None) -> bool:
    """Seed `platform_toolsets.cli` into the config file, IF it is not there.

    Returns True if the file changed. Seed, not set: once the key exists the
    file is authoritative — an operator edits it (or runs `hermes tools`)
    and this must not clobber their choice back on every restart. That is the
    deliberate asymmetry with `ensure_mcp_server`, whose value (a URL the pod
    derives from the environment) should track the env on every boot.

    `toolsets` None means DEFAULT_TOOLSETS.
    """
    want = list(toolsets) if toolsets else list(DEFAULT_TOOLSETS)
    lines = config_path.read_text().splitlines() if config_path.exists() else []

    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "platform_toolsets:"), None)
    if start is None:
        new = [*lines, *( [""] if lines and lines[-1].strip() else [] ), *render_toolsets_block(want)]
        _write(config_path, new)
        log.info("hermes config: seeded platform_toolsets.cli -> %s", ", ".join(want))
        return True

    # The block runs to the next line at column 0 that is not blank — the same
    # indentation rule hermes' own writer produces.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[0].isspace():
            end = i
            break
    block = lines[start:end]
    if any(ln.strip().startswith("cli:") for ln in block):
        log.info("hermes config: platform_toolsets.cli already set; the file owns it")
        return False

    # platform_toolsets: exists but cli: does not — add the key under it.
    block = block + ["  cli:", *(f"    - {t}" for t in want)]
    _write(config_path, [*lines[:start], *block, *lines[end:]])
    log.info("hermes config: added platform_toolsets.cli -> %s", ", ".join(want))
    return True


def resolve_toolsets(cfg: dict | None) -> list[str]:
    """The toolsets ONE session should carry, from the config hermes reads.

    Precedence — the first source that yields a non-empty list wins:

    1. hermes' own resolver, `hermes_cli.tools_config._get_platform_tools` —
       the same function the CLI uses on itself. It reads
       `platform_toolsets.cli` from the config, expands composite toolset
       names, honours `agent.disabled_toolsets`, and appends `mcp-<name>`
       for every enabled `mcp_servers` entry — so the MCP toolsets never
       have to be listed in the file by hand.
    2. the raw `platform_toolsets.cli` list from the config. For the case
       where hermes moved or renamed its resolver: an explicit list in the
       file is still worth more than a built-in guess.
    3. DEFAULT_TOOLSETS — for a config with no key at all.
    """
    cfg = cfg or {}
    try:
        from hermes_cli.tools_config import _get_platform_tools

        got = sorted(_get_platform_tools(cfg, "cli"))
        if got:
            return got
    except Exception:  # noqa: BLE001 — resolver moved/absent; the fallbacks below hold
        log.debug("hermes toolset resolver unavailable", exc_info=True)

    raw = (cfg.get("platform_toolsets") or {}).get("cli")
    if isinstance(raw, list):
        got = [str(t).strip() for t in raw if str(t).strip()]
        if got:
            return got

    return list(DEFAULT_TOOLSETS)


# ── auxiliary compression model ─────────────────────────────────────────────

# How long one compression may take before hermes gives up on it.
COMPRESSION_TIMEOUT_S = int(os.environ.get("DEEPWIKI_COMPRESSION_TIMEOUT_S", "240"))


def _compression_keys(model: str, base_url: str, context_length: int) -> list[str]:
    # provider: custom is load-bearing, not decoration: the resolver honours a
    # config base_url only when api_key is non-empty OR provider != auto —
    # and the setup wizard's template leaves `provider: auto`, which made it
    # return ("auto", model, None, None) and fall back to the main runtime,
    # i.e. the very fallback this seed exists to override.
    return [
        "    provider: custom",
        f"    model: {model}",
        f"    base_url: {base_url}",
        f"    context_length: {context_length}",
    ]


# ── the two agent knobs the DEPLOYMENT owns ─────────────────────────────────

# ATTEMPTS, not retries — `while retry_count < max_retries` in
# agent/conversation_loop.py. So 1 means no retry at all, and 3 (what the live
# config shipped) is what turned a 120 s deadline into a 6-minute wait.
#
# 2 is one retry: a transient timeout or reset still recovers, which is worth
# having — the alternative is that any single blip kills a turn — while a hard
# failure costs two deadlines instead of three. 1 was the first value here and
# it was too aggressive: it bought 120 s off the worst case by removing the
# protection entirely.
API_MAX_RETRIES = int(os.environ.get("DEEPWIKI_API_MAX_RETRIES", "2"))

# The fraction of the context window at which hermes compresses — AND IT IS
# CLAMPED BY A FLOOR THAT DOMINATES IT HERE, so lowering it does nothing:
#
#     threshold_tokens = max(int(context_length * threshold_percent),
#                            MINIMUM_CONTEXT_LENGTH)      # context_compressor
#
# MINIMUM_CONTEXT_LENGTH is 32000 in this deployment (webui.yaml seds it down
# so hermes stops refusing a 54K model), and 54000 x 0.50 = 27,000 < 32,000.
# So the default already compresses at 32,000, and 0.35 (18,900) compresses at
# 32,000 too — measured: the trigger sat at 32,917 / 35,279 / 33,275 tokens
# across runs before and after the change, which is that floor plus one turn's
# overshoot. Only a value ABOVE 0.593 moves it, and upward is the wrong way.
#
# Left at hermes' own default because a number that changes nothing is worse
# than no number: the next reader takes it for the trigger point. To compress
# earlier the lever is HERMES_MIN_CONTEXT, which also lowers the window size
# hermes will refuse a model for — a coupling webui.yaml documents.
COMPRESSION_THRESHOLD = float(os.environ.get("DEEPWIKI_COMPRESSION_THRESHOLD", "0.50"))


def ensure_agent_tuning(
    config_path: Path,
    api_max_retries: int | None = None,
    compression_threshold: float | None = None,
) -> bool:
    """SET `agent.api_max_retries` and `agent.compression.threshold`.

    SET, not seed — deliberately unlike the rest of this module. These two are
    the deployment's to decide, like the MCP url: they are derived from what
    this cluster's engines can actually do, and a value left in the PVC from an
    older boot is exactly the drift the skills sync in webui.yaml exists to
    prevent ("a version of the skill that no commit describes"). A hand-edited
    config.yaml would have been the same mistake in a different file.

    WHY THESE TWO NUMBERS.

    api_max_retries: 3 -> 1. The retry count multiplies every deadline. One
    failed context compression measured 723 s of a conversation showing
    回复中… — 3 attempts x the configured 120 s timeout, then hermes' own
    single retry of the whole call. The engine was not wedged and the deadline
    was not wrong; it was being spent six times. One attempt keeps a failure at
    120 s, and a compression that fails twice in a row is information, not
    something to grind on.

    compression.threshold: 0.50 -> 0.35. This is the opposite of the intuition
    that compression should wait longer. Compression is the EXPENSIVE step
    here: it hands the whole window to an engine that generates at ~16 tok/s,
    and it was timing out. Firing later means summarising more, which is slower
    and likelier to fail; firing earlier means a smaller job that finishes.
    The observed compressions began at 32,917 and 35,279 tokens of a 54,000
    window — 61% and 65%, because the check runs after a turn's messages land,
    so the effective point always overshoots the setting.
    """
    retries = int(api_max_retries if api_max_retries is not None else API_MAX_RETRIES)
    thresh = float(compression_threshold if compression_threshold is not None
                   else COMPRESSION_THRESHOLD)
    lines = config_path.read_text().splitlines() if config_path.exists() else []

    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "agent:"), None)
    if start is None:
        new = [*lines, *([""] if lines and lines[-1].strip() else []),
               "agent:", f"  api_max_retries: {retries}",
               "  compression:", f"    threshold: {thresh}"]
        _write(config_path, new)
        log.info("hermes config: added agent.api_max_retries=%s, compression.threshold=%s",
                 retries, thresh)
        return True

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i][0].isspace():
            end = i
            break
    block = lines[start:end]
    before = list(block)

    # api_max_retries sits directly under `agent:`.
    want_retries = f"  api_max_retries: {retries}"
    at = next((i for i, ln in enumerate(block)
               if ln.strip().startswith("api_max_retries:")
               and len(ln) - len(ln.lstrip()) == 2), None)
    if at is None:
        block.insert(1, want_retries)
    else:
        block[at] = want_retries

    # agent.compression.threshold is one level deeper.
    comp = next((i for i, ln in enumerate(block) if ln.strip() == "compression:"
                 and len(ln) - len(ln.lstrip()) == 2), None)
    want_thresh = f"    threshold: {thresh}"
    if comp is None:
        block.extend(["  compression:", want_thresh])
    else:
        stop = len(block)
        for i in range(comp + 1, len(block)):
            if block[i].strip() and len(block[i]) - len(block[i].lstrip()) <= 2:
                stop = i
                break
        tat = next((i for i in range(comp + 1, stop)
                    if block[i].strip().startswith("threshold:")), None)
        if tat is None:
            block.insert(stop, want_thresh)
        else:
            block[tat] = want_thresh

    if block == before:
        return False                      # already correct — leave the file alone
    _write(config_path, [*lines[:start], *block, *lines[end:]])
    log.info("hermes config: set agent.api_max_retries=%s, compression.threshold=%s",
             retries, thresh)
    return True


def ensure_compression_timeout(config_path: Path, seconds: int | None = None) -> bool:
    """Give `auxiliary.compression` a deadline IF it has none. Returns True if
    the file changed.

    Separate from `ensure_compression_model`, and deliberately so. That one
    stops at the first sign the operator owns the block — which the live
    config does — so a deadline added there would never reach a deployment
    that already names a compression model. A timeout is not an identity
    choice: adding one where there is none overrides nobody, and an existing
    value is left exactly alone.

    READ THE ARITHMETIC BEFORE TRUSTING THIS NUMBER. The configured value is
    per HTTP ATTEMPT, not per compression. The OpenAI SDK retries twice by
    default (three attempts), and hermes retries the whole call once on the
    same provider, so the wall-clock ceiling is SIX times what is written
    here. That is not a guess — it is what the failure measured:

        02:57:24 compression started, 35,279 tokens
        03:03:26 Request timed out          362 s = 3 x the configured 120
        03:09:27 Request timed out again    361 s = the one hermes retry
        03:09:27 all fallbacks exhausted -> a placeholder marker

    twelve minutes of a conversation showing 回复中… , and because the summary
    never landed, 26 messages stayed 26 and the window was still full.

    So a deadline here CANNOT be tightened into a fast failure: this
    deployment's config already carried `timeout: 120` (hermes' setup wizard
    writes it), and cutting it further would only make compression fail
    sooner and more often — a healthy compression needs ~180 s here (35K of
    prefill plus a 2,000-token summary at the ~16 tok/s this engine
    generates), so anything under that never succeeds at all. The auxiliary
    client exposes no retry count to turn down.

    What this seed is actually for, then, is the deployment that has NO
    timeout at all, where the default is whatever the SDK picks. The real
    defence against the twelve minutes is spending less of the window in the
    first place, so compression has less to chew and finishes inside one
    attempt — see tool_budget, and the per-WAVE budget in particular.
    """
    want = int(seconds if seconds is not None else COMPRESSION_TIMEOUT_S)
    if not config_path.exists():
        return False
    lines = config_path.read_text().splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "auxiliary:"), None)
    if start is None:
        return False
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i][0].isspace():
            end = i
            break
    block = lines[start:end]
    comp = next((i for i, ln in enumerate(block) if ln.strip() == "compression:"), None)
    if comp is None:
        return False
    stop = len(block)
    for i in range(comp + 1, len(block)):
        if block[i].strip() and len(block[i]) - len(block[i].lstrip()) < 4:
            stop = i
            break
    if any(block[i].strip().startswith("timeout:") for i in range(comp + 1, stop)):
        return False                      # the operator's, or already seeded
    block.insert(stop, f"    timeout: {want}")
    _write(config_path, [*lines[:start], *block, *lines[end:]])
    log.info("hermes config: seeded auxiliary.compression.timeout -> %ss", want)
    return True


def ensure_compression_model(config_path: Path, endpoints, min_context: int | None = None) -> bool:
    """Seed `auxiliary.compression` so compression survives a small endpoint.

    hermes summarises overflowing history through an auxiliary model and, with
    nothing configured, falls back to the ACTIVE endpoint's model — then
    refuses the whole session when that model's window is under hermes' 32K
    floor. The MiniMax endpoint exposed this for real: freetoken sizes
    max_model_len from its KV budget, and MHA KV at ~244 KiB/token means a
    small GPU seats a tiny window no flag can honestly raise (the engine ran
    on a 4090 whose ~2.9 GiB spare gave 4K — below hermes' own system prompt;
    it has since moved to an H20, but any endpoint can be the next small
    one). Pointing compression at the first endpoint that declares at least
    `min_context` keeps a small-window endpoint usable as a chat model
    without making its engine lie about the window.

    Seed, not set, like the toolsets: a `compression:` mapping that already
    has any key is the operator's choice and is never rewritten. Returns True
    if the file changed.
    """
    if min_context is None:
        try:
            from agent.model_metadata import MINIMUM_CONTEXT_LENGTH as min_context
        except Exception:  # noqa: BLE001 — hermes moved it; the deploy's floor holds
            min_context = 32000

    ep = next(
        (e for e in (endpoints or [])
         if getattr(e, "context", 0) and e.context >= min_context),
        None,
    )
    if ep is None:
        log.info("hermes config: no endpoint with context >= %s; compression stays unseeded", min_context)
        return False

    keys = _compression_keys(ep.model, ep.base_url, ep.context)
    lines = config_path.read_text().splitlines() if config_path.exists() else []
    start = next((i for i, ln in enumerate(lines) if ln.rstrip() == "auxiliary:"), None)
    if start is None:
        new = [*lines, *([""] if lines and lines[-1].strip() else []),
               "auxiliary:", "  compression:", *keys]
        _write(config_path, new)
        log.info("hermes config: seeded auxiliary.compression -> %s @ %s (%s tokens)",
                 ep.model, ep.base_url, ep.context)
        return True

    # The block runs to the next line at column 0 that is not blank — the
    # same indentation rule hermes' own writer produces.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[0].isspace():
            end = i
            break
    block = lines[start:end]
    comp = next((i for i, ln in enumerate(block) if ln.strip() == "compression:"), None)
    if comp is None:
        # Dangling `auxiliary:` (or one holding other tasks): the seed belongs
        # UNDER the existing key, not as a second block.
        block = block + ["  compression:", *keys]
    else:
        # compression:'s children sit at indent >= 4; the next task key
        # (`  vision:` et al, indent 2) ends the subsection.
        stop = len(block)
        for i in range(comp + 1, len(block)):
            if block[i].strip() and len(block[i]) - len(block[i].lstrip()) < 4:
                stop = i
                break
        if any(block[i].strip() for i in range(comp + 1, stop)):
            # hermes' own setup wizard writes an EMPTY template (provider:
            # auto, model/base_url/api_key: '') — an absence wearing a
            # mapping, not a choice. Only the four keys that actually steer
            # _resolve_task_provider_model count: timeout/extra_body carry
            # non-empty defaults (`timeout: 120`) that say nothing about
            # identity, so a generic non-empty check would honour the
            # template forever and the seed would never fire.
            identity = ("provider", "model", "base_url", "api_key")

            def _names_something(ln: str) -> bool:
                key = ln.strip().partition(":")[0].strip()
                if key not in identity:
                    return False
                v = ln.partition(":")[2].strip().strip("'\"")
                return bool(v) and v.lower() != "auto"
            if any(_names_something(block[i]) for i in range(comp + 1, stop)):
                log.info("hermes config: auxiliary.compression already set; the file owns it")
                return False
            # Empty template: fill in place — replace the model/base_url
            # lines it already has (a duplicate `model:` key would leave
            # which one wins to the parser) and add context_length.
            for key_line in keys:
                k = key_line.strip().partition(":")[0]
                at = next((i for i in range(comp + 1, stop)
                           if block[i].strip().startswith(f"{k}:")), None)
                if at is not None:
                    block[at] = key_line
                else:
                    block.insert(stop, key_line)
                    stop += 1
        else:
            block = [*block[: comp + 1], *keys, *block[stop:]]

    _write(config_path, [*lines[:start], *block, *lines[end:]])
    log.info("hermes config: seeded auxiliary.compression -> %s @ %s (%s tokens)",
             ep.model, ep.base_url, ep.context)
    return True


# NOTE: an `ensure_disabled_toolsets` used to live here and was REMOVED, because
# it did not work on the ACP path: `acp_adapter/session.py` hardcoded its
# toolset list, so nothing in config.yaml reached the session and only
# patch_acp_toolsets.py could change it. The library path has no such hole:
# `build_agent` resolves the toolsets with `resolve_toolsets()` (hermes' own
# `_get_platform_tools`, which HONOURS `agent.disabled_toolsets`) and hands
# the result to `AIAgent.__init__` directly. Trimming the toolset is now a
# config.yaml edit — no patch, no env. The Dockerfile still runs
# patch_acp_toolsets.py as belt-and-braces for the retired ACP path; it no
# longer gates anything here.
#
# What config.yaml IS still needed for: the same `mcp_servers` block both
# names the `mcp-<name>` toolsets (the resolver appends them) and connects
# the servers (`register_mcp_servers` at startup). Either alone yields an
# agent with no retrieval — see W10.
