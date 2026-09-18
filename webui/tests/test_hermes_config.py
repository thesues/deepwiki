"""`hermes_config`: the config file owns what a session can do and with what.

`ensure_mcp_server` has been covered indirectly by the deploy for a while;
these tests pin the seeders it grew afterwards: the toolset list, and the
auxiliary compression model (hermes refuses a session whose compression
model cannot hold its 32K floor, and the slot defaults to the ACTIVE
endpoint — so a small-window endpoint must be served by a declared bigger
one). In both cases the seed must not clobber, the resolution must degrade
in order, and terminal must be in the default — an agent that cannot run
anything is not a degraded agent, it is a broken one.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hermes_config as hc  # noqa: E402


# ── ensure_platform_toolsets ────────────────────────────────────────────────


def test_a_config_without_the_key_gets_seeded(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("model:\n  provider: custom\n  base_url: http://a/v1\n")

    assert hc.ensure_platform_toolsets(p, ["file", "terminal"]) is True
    text = p.read_text()
    assert "platform_toolsets:" in text and "  cli:" in text
    assert "    - terminal" in text
    # The block this touched is the only one that moved.
    assert "provider: custom" in text and "base_url: http://a/v1" in text


def test_an_existing_list_is_never_clobbered(tmp_path):
    """Seed, not set. An operator (or `hermes tools`) wrote a choice; a pod
    restart must not silently overwrite it back to the default."""
    p = tmp_path / "config.yaml"
    p.write_text("platform_toolsets:\n  cli:\n    - file\n")

    assert hc.ensure_platform_toolsets(p, ["everything"]) is False
    assert "- everything" not in p.read_text()
    assert "- file" in p.read_text()


def test_seeding_is_idempotent(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("mcp_servers:\n  memory:\n    url: http://mcp\n")

    assert hc.ensure_platform_toolsets(p, ["file", "terminal"]) is True
    first = p.read_text()
    assert hc.ensure_platform_toolsets(p, ["file", "terminal"]) is False
    assert p.read_text() == first


def test_an_inline_cli_value_counts_as_configured(tmp_path):
    """`cli: [file, terminal]` — flow style, which hermes also writes — is a
    list the file owns, not an absence to seed over."""
    p = tmp_path / "config.yaml"
    p.write_text("platform_toolsets:\n  cli: [file, terminal]\n")

    assert hc.ensure_platform_toolsets(p, None) is False
    assert p.read_text().count("cli:") == 1


def test_an_empty_toolsets_argument_seeds_the_default(tmp_path):
    p = tmp_path / "config.yaml"
    assert hc.ensure_platform_toolsets(p, None) is True
    text = p.read_text()
    assert "    - terminal" in text, "the default must keep terminal"


def test_a_dangling_platform_toolsets_key_still_gets_cli(tmp_path):
    """`platform_toolsets:` present but empty — a hand edit, not a hermes
    write. The seed belongs UNDER the existing key, not as a second block."""
    p = tmp_path / "config.yaml"
    p.write_text("platform_toolsets:\n")

    assert hc.ensure_platform_toolsets(p, ["file"]) is True
    text = p.read_text()
    assert text.count("platform_toolsets:") == 1
    assert "  cli:" in text and "    - file" in text


# ── ensure_compression_model ────────────────────────────────────────────────


class _EP:
    """Endpoint stand-in: only the fields the compression seeding reads."""

    def __init__(self, context, model="m", base_url="http://b/v1"):
        self.context = context
        self.model = model
        self.base_url = base_url


def test_compression_seeds_from_the_first_qualifying_endpoint(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("mcp_servers:\n  memory:\n    url: http://mcp\n")
    eps = [_EP(4096, model="small", base_url="http://small/v1"),
           _EP(62080, model="big", base_url="http://big/v1")]

    assert hc.ensure_compression_model(p, eps, min_context=32000) is True
    text = p.read_text()
    assert "  compression:" in text
    assert "    model: big" in text and "    base_url: http://big/v1" in text
    assert "    context_length: 62080" in text
    assert "url: http://mcp" in text, "the block this touched is the only one that moved"


def test_compression_skips_when_no_endpoint_qualifies(tmp_path):
    p = tmp_path / "config.yaml"

    assert hc.ensure_compression_model(p, [_EP(4096)], min_context=32000) is False
    assert not p.exists(), "nothing to seed means nothing to write"


def test_compression_never_clobbers_an_operators_choice(tmp_path):
    """Seed, not set — same contract as platform_toolsets. A model-only
    mapping is a deliberate override even if it omits base_url."""
    p = tmp_path / "config.yaml"
    p.write_text("auxiliary:\n  compression:\n    model: mine\n")

    assert hc.ensure_compression_model(p, [_EP(62080)], min_context=32000) is False
    assert "model: mine" in p.read_text()


def test_compression_fills_under_an_existing_auxiliary_key(tmp_path):
    """Other auxiliary tasks (vision, web_extract) must survive."""
    p = tmp_path / "config.yaml"
    p.write_text("auxiliary:\n  vision:\n    provider: auto\n")

    assert hc.ensure_compression_model(
        p, [_EP(62080, model="big", base_url="http://big/v1")], min_context=32000
    ) is True
    text = p.read_text()
    assert text.count("auxiliary:") == 1
    assert "  vision:" in text and "    provider: auto" in text
    assert "  compression:" in text and "    model: big" in text


def test_compression_fills_an_empty_compression_key(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("auxiliary:\n  compression:\n")

    assert hc.ensure_compression_model(
        p, [_EP(62080, model="big", base_url="http://big/v1")], min_context=32000
    ) is True
    text = p.read_text()
    assert text.count("compression:") == 1
    assert "    model: big" in text


def test_compression_fills_the_wizards_empty_template_in_place(tmp_path):
    """hermes' setup wizard writes provider: auto and empty-string values —
    an absence wearing a mapping. The seed must fill THAT (replacing the
    empty model/base_url lines, no duplicate keys) instead of honouring it,
    or the slot keeps falling back to the active endpoint's model."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "auxiliary:\n"
        "  compression:\n"
        "    provider: auto\n"
        "    model: ''\n"
        "    base_url: ''\n"
        "    api_key: ''\n"
        "    timeout: 120\n"
        "    extra_body: {}\n"
    )

    assert hc.ensure_compression_model(
        p, [_EP(62080, model="big", base_url="http://big/v1")], min_context=32000
    ) is True
    text = p.read_text()
    assert text.count("model:") == 1 and "    model: big" in text
    assert text.count("base_url:") == 1 and "    base_url: http://big/v1" in text
    assert "    context_length: 62080" in text
    # provider must be REWRITTEN, not kept: with `auto` the resolver ignores
    # the seeded base_url entirely and falls back to the main runtime.
    assert text.count("provider:") == 1 and "    provider: custom" in text
    assert "    api_key: ''" in text, "sibling keys stay"
    assert "    timeout: 120" in text and "    extra_body: {}" in text, "template plumbing stays"
    # and the template's non-empty-but-generic timeout must NOT be mistaken
    # for an operator choice that blocks the seed
    assert "    context_length: 62080" in text


def test_compression_seeding_is_idempotent(tmp_path):
    p = tmp_path / "config.yaml"
    eps = [_EP(62080, model="big", base_url="http://big/v1")]

    assert hc.ensure_compression_model(p, eps, min_context=32000) is True
    first = p.read_text()
    assert hc.ensure_compression_model(p, eps, min_context=32000) is False
    assert p.read_text() == first


def test_compression_floor_defaults_to_hermes_when_importable(tmp_path, monkeypatch):
    """Without an explicit floor the number must come from hermes itself, not
    a second copy that can drift."""
    fake = types.ModuleType("agent.model_metadata")
    fake.MINIMUM_CONTEXT_LENGTH = 40000
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.model_metadata", fake)

    p = tmp_path / "config.yaml"
    assert hc.ensure_compression_model(p, [_EP(32000)]) is False  # below 40K
    assert hc.ensure_compression_model(p, [_EP(40000)]) is True


# ── resolve_toolsets ────────────────────────────────────────────────────────


def test_resolution_prefers_hermes_own_resolver(monkeypatch):
    """The same function the CLI uses on itself — including its `mcp-<name>`
    appending. If deepwiki re-derived that by hand, the two ends would drift."""
    fake = types.ModuleType("hermes_cli.tools_config")
    fake._get_platform_tools = lambda cfg, platform, **kw: {"file", "terminal", "mcp-memory"}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.tools_config", fake)

    assert hc.resolve_toolsets({"mcp_servers": {"memory": {}}}) == [
        "file", "mcp-memory", "terminal",
    ]


def test_resolution_falls_back_to_the_raw_config_list(monkeypatch):
    """hermes moved its resolver — an explicit list in the file is still worth
    more than a built-in guess."""
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # import fails
    cfg = {"platform_toolsets": {"cli": ["file", " terminal ", ""]}}
    assert hc.resolve_toolsets(cfg) == ["file", "terminal"]


def test_resolution_ends_at_the_default_which_keeps_terminal(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    got = hc.resolve_toolsets({})
    assert "terminal" in got and "file" in got
    assert got == hc.DEFAULT_TOOLSETS


def test_resolution_never_returns_an_empty_list(monkeypatch):
    """An empty `cli: []` in the file is 'misconfigured', not 'no tools': the
    honest degradation is the default, because an agent with no toolsets
    cannot even read the corpus it exists to search."""
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    cfg = {"platform_toolsets": {"cli": []}}
    assert hc.resolve_toolsets(cfg) == hc.DEFAULT_TOOLSETS


def test_the_compression_deadline_is_seeded_where_there_is_none(tmp_path):
    """The live deployment's compression block names a model, so
    `ensure_compression_model` stops at "the file owns it" — and a deadline
    added there would never reach it. This one runs anyway, because a timeout
    is not an identity choice.

    Ablation: fold this back into ensure_compression_model and the owned-block
    case goes uncovered, which is exactly the deployment that burned twelve
    minutes per failed compression.
    """
    p = tmp_path / "config.yaml"
    p.write_text(
        "auxiliary:\n"
        "  compression:\n"
        "    provider: custom\n"
        "    model: dsv4-flash\n"
        "    base_url: http://freetoken-l3:1919/v1\n"
        "  vision:\n"
        "    provider: auto\n"
    )
    assert hc.ensure_compression_timeout(p, 240) is True
    text = p.read_text()
    assert "    timeout: 240" in text
    # It belongs to compression, not to the task that follows it.
    assert text.index("timeout: 240") < text.index("  vision:")
    # Idempotent: a restart loop must not churn a file hermes is reading.
    assert hc.ensure_compression_timeout(p, 240) is False


def test_an_operators_deadline_is_never_overwritten(tmp_path):
    """Seed, don't set — the same contract the rest of this module keeps."""
    p = tmp_path / "config.yaml"
    p.write_text(
        "auxiliary:\n"
        "  compression:\n"
        "    provider: custom\n"
        "    model: big\n"
        "    timeout: 90\n"
    )
    assert hc.ensure_compression_timeout(p, 240) is False
    assert "    timeout: 90" in p.read_text()


def test_the_deadline_seed_is_quiet_when_there_is_nothing_to_seed(tmp_path):
    """No file, no auxiliary block, no compression key: all no-ops, because
    ensure_compression_model owns creating the block."""
    missing = tmp_path / "nope.yaml"
    assert hc.ensure_compression_timeout(missing, 240) is False
    p = tmp_path / "c.yaml"
    p.write_text("model:\n  default: x\n")
    assert hc.ensure_compression_timeout(p, 240) is False
    p.write_text("auxiliary:\n  vision:\n    provider: auto\n")
    assert hc.ensure_compression_timeout(p, 240) is False


def test_agent_tuning_rewrites_the_live_shape(tmp_path):
    """The live config's shape exactly: an `agent:` block that already carries
    api_max_retries (3 — the multiplier that turned a 120 s deadline into 723 s
    of 回复中…) and no compression block at all.
    """
    p = tmp_path / "config.yaml"
    p.write_text(
        "model:\n  default: dsv4-flash\n"
        "agent:\n"
        "  max_turns: 90\n"
        "  gateway_timeout: 1800\n"
        "  api_max_retries: 3\n"
        "auxiliary:\n  compression:\n    provider: custom\n"
    )
    assert hc.ensure_agent_tuning(p, 1, 0.35) is True
    text = p.read_text()
    assert "  api_max_retries: 1" in text and "api_max_retries: 3" not in text
    assert "  compression:\n    threshold: 0.35" in text
    # Siblings survive, and the neighbouring top-level blocks are untouched.
    assert "  max_turns: 90" in text and "  gateway_timeout: 1800" in text
    assert "auxiliary:\n  compression:\n    provider: custom" in text
    # agent.compression must not be confused with auxiliary.compression: the
    # threshold belongs under the FIRST one.
    assert text.index("threshold: 0.35") < text.index("auxiliary:")
    # Idempotent: a restart loop must not churn a file hermes is reading.
    assert hc.ensure_agent_tuning(p, 1, 0.35) is False


def test_agent_tuning_is_a_set_not_a_seed(tmp_path):
    """Unlike the rest of this module. These two are derived from what this
    cluster's engines can do, so a value left over from an older boot is drift
    — the same drift the skills sync exists to prevent."""
    p = tmp_path / "config.yaml"
    p.write_text("agent:\n  api_max_retries: 9\n  compression:\n    threshold: 0.9\n")
    assert hc.ensure_agent_tuning(p, 1, 0.35) is True
    text = p.read_text()
    assert "  api_max_retries: 1" in text and "threshold: 0.35" in text
    assert "0.9" not in text and "api_max_retries: 9" not in text


def test_agent_tuning_creates_the_block_when_absent(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("model:\n  default: x\n")
    assert hc.ensure_agent_tuning(p, 1, 0.35) is True
    text = p.read_text()
    assert "agent:" in text and "  api_max_retries: 1" in text
    assert "  compression:\n    threshold: 0.35" in text
    assert "model:\n  default: x" in text
