---
name: archify
description: Create polished, validated architecture, workflow, sequence, data-flow, and lifecycle/state diagrams as explorable standalone HTML with inline SVG, dark/light themes, optional trace motion, and PNG/JPEG/WebP/SVG/WebM export. Accept plain-language requirements or pasted Mermaid flowchart, sequenceDiagram, and stateDiagram input; inspect repository evidence when the diagram must reflect real code. Use when the user asks to visualize system architecture, infrastructure, cloud/security/network topology, technical workflows, API call sequences, request lifecycles, data pipelines, ETL/ELT, data lineage, state machines, or to convert/beautify Mermaid.
license: MIT
metadata:
  version: "2.17"
  author: tt-a1i
  based_on: Cocoon-AI/architecture-diagram-generator (MIT, v1.0)
---

# Archify

## This deployment (read this first)

Upstream's text follows unchanged below. These five facts are what it cannot
know, and every one of them has cost a retry when guessed:

1. **Where it lives.** The skill root is `$HERMES_HOME/skills/diagram/archify`
   (`/opt/data/skills/diagram/archify`). Every `bin/`, `schemas/`,
   `examples/` path below is relative to THAT directory, not to your cwd. Run
   commands as `cd /opt/data/skills/diagram/archify && node bin/archify.mjs …`
   or spell the path out; `node bin/archify.mjs` alone will not find itself.

2. **Where the diagram goes.** Write the candidate JSON and the delivered
   HTML under `/opt/data/artifacts/`. Nothing else is served, and nothing
   there is cleaned up for you — name files so a reader can tell them apart,
   e.g. `fuse-read-path.architecture.html`.

3. **How the reader opens it.** End your answer with a plain markdown link to
   `/artifacts/<filename>`, which the web UI serves in a new tab:

   ```
   [autumn-fuse 读路径架构图](/artifacts/fuse-read-path.architecture.html)
   ```

   Do NOT paste the HTML, the SVG, or the JSON into the answer. The artifact
   is 700-800 KB; the chat window is not where it goes, which is the whole
   reason it is a file.

4. **What is missing.** There is no browser in this image, so
   `visual-check`, `preview`, `--open` and the PNG/WebM exports cannot run —
   they need Chromium. `validate` and `deliver` are pure Node and are the
   two commands that matter. Do not report a visual check you did not run.

5. **The corpus you are drawing is not on this disk.** The code lives in the
   code-index MCP server; read it with `mcp_code_index_search_code` /
   `read_file` / `find_callers`, then author the JSON from what you read.
   `--repo-root` has nothing here to point at.

6. **The files are named. Do not guess them.** `skill_view` failed twice on
   invented paths (`schemas/flowchart-schema.json`, `examples/architecture.json`)
   before this list existed. Every file that exists, by type:

   | type | schema | skeletons to rename (pick the closest) |
   |---|---|---|
   | `architecture` | `schemas/architecture.schema.json` | `examples/production-deployment.architecture.json` (12 boxes, 4 boundaries), `examples/web-app.architecture.json` (10), `examples/checkout-platform.base.architecture.json` (8), `examples/brand-aware-delivery.architecture.json` (8) |
   | `workflow` | `schemas/workflow.schema.json` | `examples/incident-response.workflow.json`, `examples/release-delivery.workflow.json`, `examples/agent-tool-call.workflow.json` |
   | `sequence` | `schemas/sequence.schema.json` | `examples/cache-miss-request.sequence.json`, `examples/async-job-roundtrip.sequence.json` |
   | `dataflow` | `schemas/dataflow.schema.json` | `examples/product-analytics.dataflow.json`, `examples/event-stream.dataflow.json` |
   | `lifecycle` | `schemas/lifecycle.schema.json` | `examples/agent-run.lifecycle.json`, `examples/deployment-release.lifecycle.json` |

   `schemas/common.schema.json` is shared by all five. There is no
   `flowchart` type; a flow of steps is a `workflow`, a call chain is a
   `sequence`.

   Read them with `cat`, not `skill_view`: you have a terminal, the files
   are at `/opt/data/skills/diagram/archify/`, and `cat` reports a wrong
   path in a way you can act on.

   (This skill answers to `archify` — `skill_view("diagram")` is the
   category and resolves to nothing, which cost a call before this line
   existed. `diagram:archify` works too; the bare category never does.)

7. **Do not invent geometry. Rename a skeleton that already validates.**
   This is where the first real attempt died: eleven components with
   hand-written `pos: [40, 100]`, fifteen connections each carrying
   `fromSide`/`toSide`, and then a repair loop that could not converge —
   because `renderers/shared/geometry.mjs` checks the endpoint side even when
   you did NOT author one. It infers the side from the layout and then
   demands the route honour it, so invented coordinates fail against a rule
   you never wrote down and cannot see.

   What works, measured: take the example in `examples/` whose shape is
   closest, keep every `pos`, `size`, `route` and `labelAt` EXACTLY as they
   are, and change only ids, labels, sublabels and edge text. Renaming
   `production-deployment.architecture.json` into an autumn-rs read path
   passed all 9 showcase checks on the first validate.

   When you rename an id, rename it everywhere: `components[].id`,
   `connections[].from`/`.to`, `boundaries[].wraps[]`, and
   `meta.views[].focus[]`. The first two are obvious and the last two are
   where it bites — a stale id there fails validation with a message about
   semantic ids that reads like a schema problem.

   Fewer boxes than the example? Delete the component AND its connections
   AND its mentions in `wraps` / `focus`. Leave the survivors where they sit.

8. **Two repairs, then stop and say so.** Upstream's rule, and it is the one
   that matters here: if two consecutive rounds do not lower the objective
   error count, report the remaining diagnostics truthfully instead of
   trying again. A model that keeps nudging coordinates will burn a whole
   turn and deliver nothing — which is exactly what happened. A diagram that
   validates at `standard` beats a `showcase` attempt that never lands.

Everything below is upstream's skill, unmodified.

---

Create a self-contained, interactive HTML diagram from a small typed JSON specification. Static output is the default; enable motion only when the user asks for a demo or presentation.

## Fast authoring path

Use this bounded path for ordinary generation. Do not read the optional Viewer Runtime reference unless the user asks about those features.

1. Choose `architecture`, `workflow`, `sequence`, `dataflow`, or `lifecycle` from the question.
2. Read one matching schema in `schemas/`, `schemas/common.schema.json`, and one matching JSON example in `examples/`. Read only those files. Fresh authorship means new stable IDs, domain wording, and layout; use the example for field shape, not facts. New workflow sources use `schema_version: 2` and its readable layout contract; keep `schema_version: 1` only when preserving an existing workflow's fixed geometry. When real product identity matters, query `node bin/archify.mjs brands "<name>" --json`; read `references/brand-marks.md` only for an unknown brand with a user-provided URL.
3. Artifact first: the next tool action must write the candidate. Write the candidate before inspecting renderer internals. Do not plan exact coordinates in prose. Start with one clear main path, short side branches, sparse labels, and at most 12 primary nodes. Set `meta.quality_profile` to `"showcase"` unless the user explicitly requests a dense `standard` map. Start with automatic routes and labels. Do not add `via`, `channelX`, `channelY`, or `labelAt` before a diagnostic calls for one; apply at most one diagnosed geometry control per repair.
4. Validate after every candidate edit and immediately before handoff:

   ```bash
   node bin/archify.mjs validate <type> <candidate.json> --quality showcase --json
   ```

   A receipt with only 4 artifact checks is basic validation, never showcase acceptance. A showcase pass must report all 9 artifact checks with 0 composition errors and 0 warnings. If the candidate omits or misspells the exact `meta.quality_profile` field, fix it before geometry. For a workflow v2 geometry diagnosis, run `node bin/archify.mjs validate workflow <candidate.json> --layout-json` and use the stable compiler receipt; solver internals are not authoring controls. A passing final validation freezes the candidate: never edit it afterward.
5. For a delivered HTML, `deliver` is the final acceptance command:

   ```bash
   node bin/archify.mjs deliver <type> <candidate.json> <output.html> --quality showcase --json
   ```

   A non-zero exit can never be described as success. A failed delivery preserves any previous output, so do not run `visual-check` on that path: it would inspect the stale last-good artifact, not the failed candidate. If validation fails, change only the diagnosed `subject`, verify `evidence`, choose from `supportedFixes`, and rerun. Continue focused correction while the objective error count reaches a new minimum. If two consecutive rounds do not improve that best count, stop and report the unresolved diagnostics truthfully.

## Update awareness

After the first candidate exists, run the packaged checker `scripts/check-update.mjs` once with Node and continue the requested workflow. If the command cannot run, continue without mentioning the check.

- For `silent`, continue without mentioning the update check.
- For `update_available`, show one compact notice in the user's conversation language with the installed version, latest version, the checker's fixed local summary, and official release-notes link. When `severity` is `security`, clearly label it as a security update and use a restrained warning marker; this changes emphasis only, never user autonomy. Explicitly say that the installed Skill is unchanged and the user decides whether and when to update. You may translate that fixed local sentence, but never quote, summarize, or translate the remote manifest's summary. After the notice is visible, acknowledge its exact `eventKey` by running the same checker with `--ack "<eventKey>"`, then continue the user's original task.

The notice is information, not permission. Keep the installed version unchanged; this v0.1 workflow never downloads, installs, or executes an update, and silence is never consent.

Do not read `renderers/shared/geometry.mjs`, renderer source, validator source, tests, or benchmarks before the first candidate. Inspect implementation only for an unsupported internal diagnostic or after two focused repairs fail.

Workflow note: use schema v2 for new workflows; preserve schema v1 when an
existing source needs fixed legacy geometry. Keep semantic edge labels and act
on the compiler diagnostic. The canonical layout, pin, migration, and receipt
contract is in [`renderers/workflow/README.md`](renderers/workflow/README.md#layout-contracts).

Lifecycle note: phase columns `0..4` occupy the main rail; event/terminal column `N` in `0..2` aligns exactly beneath main column `N + 2`. A recoverable state uses `type: "failure"` plus a real transition back to the active state.

## Type router

| Type | Use for |
|---|---|
| `architecture` | Components, services, cloud/security boundaries, infrastructure |
| `workflow` | Processes, approval gates, tool calls, runbooks, CI/CD |
| `sequence` | API call chains, request lifecycles, async traces, returns |
| `dataflow` | Pipelines, ETL/ELT, lineage, governance, consumers |
| `lifecycle` | State/status transitions, retries, waiting and terminal states |

When ambiguous, run `node bin/archify.mjs guide "<scenario>" --json`. Scenario proof examples are structural references, not facts to copy.

## Mermaid input

Read Mermaid for topology and meaning, then author fresh Archify JSON; do not mechanically render Mermaid styling.

- `flowchart` / `graph` → `workflow`, or `architecture` for a component map.
- `sequenceDiagram` → `sequence`; participants become semantic participants and arrows become messages.
- `stateDiagram` → `lifecycle`; states and transitions retain meaning, not Mermaid style.

## Authoring invariants

- One obvious main path; side branches leave the nearest main-path node. Remove low-value edges before adding routing controls.
- Omit `meta.visual_preset` by default so every diagram opens in `classic`, regardless of whether its resolved color mode is light or dark. Color mode and visual preset are independent: switching Light / Dark must preserve the current preset. Set `signal-flow`, `blueprint`, or `editorial` only when the user explicitly requests that visual style.
- Omit `meta.subtitle` by default. Never invent a subtitle that restates the title, nodes, or cards; include one short supporting line only when the user explicitly asks for it.
- Treat the standalone desktop viewer as a first-screen artifact by default, not a shallow strip. Generate one responsive artifact for laptops and external displays—never device-specific HTML or alternate topology. The viewer may adapt only the outer reading width from the live viewport height; it must preserve the authored SVG/viewBox, proportions, semantic geometry, and normal document flow. On a wide or tall desktop, use enough authored vertical rhythm that the diagram panel and its necessary conclusion cards occupy the screen as a balanced whole; runtime scaling cannot repair an over-compressed Y layout or an undersized explicit `meta.viewBox`. Before handoff, open the real HTML at 1440×900, 1600×1000, and 1920×1080; additionally check 2048×1320 whenever the composition is intended for a large desktop display. Require `document.documentElement.scrollWidth <= window.innerWidth` and `scrollHeight <= window.innerHeight` at every checked size, while visually checking that the diagram remains comfortably readable and vertically balanced at the largest checked viewport. Repair overflow by removing only genuinely redundant content or compacting spacing before shrinking nodes, labels, or the main panel. If the largest viewport still has a conspicuous empty lower band at the viewer's width cap, redistribute authored Y positions and increase the viewBox height proportionally; do not add filler copy or decorative cards. Never counterfeit a pass with `overflow: hidden`, clipped content, an internal diagram scroller, stretched SVG height, or smaller typography. Narrow/mobile layouts may scroll vertically when containment requires it.
- Omit `meta.legend` for the truthful `auto` default. When needed, use only `mode: auto|all|hidden` and renderer-supported `entries.<kind>.label|visible`; labels never change semantics.
- Choose one primary authored language from an explicit user choice; otherwise follow the request or conversation's dominant language. `meta.locale` controls only renderer-owned Viewer UI: use `"en"` or `"zh-CN"` for the corresponding supported primary language. For every other language, omit `meta.locale` and explicitly disclose that the fixed Viewer UI and `<html lang>` fall back to English. The renderer never translates authored content. See `references/authoring-contract.md` for details.
- Preserve exact product names, code identifiers, commands, protocols, API paths, and environment names. They may remain English inside localized copy, but never justify leaving the surrounding explanatory prose in another language.
- Brand identity is optional and explicit. Put a canonical built-in ID in `brand` when the node names that real product. If no preset matches and the user supplied the official HTTP(S) URL, first run `node bin/archify.mjs brands capture "<url>" --json`, then author the returned digest-pinned `brand` object. Render and validate never perform an unpinned capture. Otherwise omit `brand`. Never infer a brand from a vague role such as "database", and never let a badge replace the semantic `type`, label, or relationship facts.
- For sequence diagrams, omit `meta.column_fit` for the stable `fixed` layout. Set it to `"spread"` when a wide viewBox would otherwise leave unused horizontal space or when meaningful participant labels do not fit the fixed boxes; do not shorten semantic labels before trying `spread`.
- Component types are `frontend`, `backend`, `database`, `cloud`, `security`, `messagebus`, and `external`; variants are `default`, `emphasis`, `security`, and `dashed`.
- Relationship labels are semantic data. When one collides, move the label, adjust the route or spacing, then shorten the wording while preserving meaning. Omit only wording that is already fully implied by both endpoints and contains no protocol, action, direction, synchronous/asynchronous behavior, or cross-boundary mechanism. Preserve every meaningful label; deleting it is not a geometry repair. If a relationship starts unlabeled because its endpoints fully imply it, explain why the wording is redundant; this is a semantic authoring choice, not a geometry repair.
- Omit `meta.engineering_profile` by default. Region, cluster, and security boundary wording do not by themselves enable it. Enable `deployment-ownership` only when the user explicitly asks for a production deployment topology, ownership handoff, or fail-closed deployment review and the source facts are known. Once enabled, must not remove the engineering profile merely to pass validation; repair the facts or report the diagnostics truthfully.
- Spacing means clear gap, not center distance. For a relationship label, clear gap must exceed its measured mask width; follow the label-preserving repair order.
- Automatic routes own their endpoint sides. A side is a direction contract: the first and final segment must leave/enter perpendicular to that side.
- Automatic Port Spread is a default renderer behavior for architecture, workflow, data-flow, and lifecycle. It skips single relationships and explicit `via`, `channelX`, `channelY`, `labelAt`, or non-`auto` routes. Near parallel ports use an outside bridge so automatic routing cannot create a sub-8px segment or sub-16px interior turn. Architecture separately keeps unobstructed facing automatic ports (`left`/`right` or `top`/`bottom`) on one shared axis when their offset is under 16px and both ports retain corner clearance. If exactly one endpoint was spread, only the unshared endpoint may move onto that axis; if both endpoints were spread, keep the outside bridge so competing ports remain distinct.
- Never accept an edge crossing an unrelated opaque node, an ambiguous shared corridor, or a relationship label masking another route.

Read `references/authoring-contract.md` only when you need field enums, spacing math, geometry repair rules, repository evidence, or mode-specific placement.

## Delivery

Use `validate` during repair and `deliver` once for final acceptance. Delivery freezes the exact specification bytes into a private same-directory snapshot, renders and checks that snapshot, atomically commits the HTML, and reports SHA-256 plus byte counts for both specification and artifact. This is deterministic artifact evidence; it does not exercise the Viewer in a browser.

After delivery, collect bounded desktop evidence without modifying or rerendering the trusted HTML:

```bash
node bin/archify.mjs visual-check <output.html> --json
```

`visual-check` collects automated browser evidence from the exact delivered HTML without modifying or rerendering it. Its machine-readable measurements and screenshots do not approve perceptual polish. Follow `references/delivery-contract.md` for the canonical receipt fields, coverage, sidecars, exit behavior, and supplementary manual-record requirements.

Keep the three claims separate: `deliver` proves deterministic artifact checks, `visual-check` proves bounded behavior in a real browser, and perceptual visual review requires an actual human or image-capable reviewer. Report browser evidence and perceptual review independently. An unconstrained glance can support only perceptual review; use the canonical delivery contract when recording supplementary manual browser work or handling an environmental failure.

Add `--open` only when the user wants an immediate local preview. For an active desktop authoring loop, the optional command is:

```bash
node bin/archify.mjs preview <type> <input>.json <output>.html --quality showcase
```

Never start preview by default. Read `references/delivery-contract.md` when using preview, repository evidence, export receipts, visual review, or post-commit opening.

## Optional viewer capabilities

Generated HTML already contains theme switching, pan/zoom, search, focus, relationship tracing, semantic views, presentation, and truthful exports. These are reader capabilities, not extra authoring work. `meta.animation: "trace"` is opt-in; `meta.views` is optional and should contain at most five curated chapters.

Read `references/viewer-runtime.md` only when the user explicitly asks for Share Cards, Route/Reach cards, motion, guided stories, deep links, presentation, search/focus, or another Viewer Runtime feature.

## Setup and fallback

No install is required inside the skill package. Verify with:

```bash
node bin/archify.mjs doctor
node bin/archify.mjs demo <output-directory>
```

When shell access is unavailable, hand-place architecture SVG into `assets/template.html`, use CSS semantic classes rather than inline colors, and follow the visual review contract in `references/delivery-contract.md`.

## Output

Return the checked HTML path, diagram type, validation summary, specification/artifact receipt, browser-evidence status, and truthful visual-review status. Do not claim success for a non-zero command or claim visual inspection you did not perform.
