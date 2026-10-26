# Agent WebUI — direct Hermes / native Kanban / independent Swarm

```sh
python -m complex.main --host 127.0.0.1 --port 8081
```

One user-facing execution selector, with actual direct and multi-Agent paths.
It reuses the shared chat/HTTP/media and Hermes library; it does not proxy to or
start another WebUI. `general.main` remains an optional standalone direct-only
service. The former custom controller, supervisor, IPC and research database
remain removed; research execution is native Kanban, not another scheduler.

## Composer execution choice

The choice lives **below the message input**, with attachments and the Agent
board, so neither the selector nor its fixed badge consumes editing width.
Choose **单体执行 / 团队协作 / 独立群研**. New sessions default to single execution.
The first direct reply locks single mode; creation of execution tasks locks
Kanban/Swarm. The selector becomes a read-only “已固定” badge and the backend
rejects changes. A different mode requires a new session; cancellation does not
unlock it. Existing research sessions retain their original role and mode.
An unsent draft's selection survives the first asynchronous identity/session-list
response; an actual identity change or new-session click resets it. Sending waits
for identity/mode readiness and captures that mode before consuming the draft.
The unified chat API requires an explicit valid `context.researchMode`; missing
or invalid context is rejected, never silently downgraded to single execution.

- **Single / 单体执行**: the ordinary Hermes Agent directly answers, codes and
  uses configured tools/MCP/domain skills. No planning card, Worker or Kanban
  dispatch is created for that conversation. `delegate_task` stays disabled.
  The handwritten general capability introduction is shown in this mode.
- **Kanban / 团队协作**: the lead decomposes the objective into assigned task cards with
  delivery/acceptance criteria and explicit dependencies. Independent cards run
  in parallel; dependent cards start when native parent dependencies complete.
  Native completed result/summary/attachments provide the handoff.
- **Swarm / 独立群研**: all workers start with identical question/baseline/budget and select
  their own direction. They repeatedly publish public stages, review other public
  results, absorb findings with provenance and continue independent research.
  There is no all-workers-finish verifier/synthesizer barrier.

Modes are applied to the backend, not just UI labels. The shared chat page has
only generic page-context/session-change hooks. Complex stores UI mode preferences
as owner/session-scoped JSON under `COMPLEX_HOME/ui-settings`, reading Hermes
compression lineage so the preference follows session rotation. This is **UI
configuration**, not a second research database or scheduler. A turn captures its
selected mode; the lead's `research_config` and `research_create` use that mode.
Execution history and persisted lock state are authoritative, so a stale browser
cannot change the mode after work has started. This lock follows session rotation.
Direct and coordinator Agents read separate configured Hermes homes using native
context-local home overrides, not process-global environment switching. Sessions
and artifact URLs stay in the unified service's existing store. Role changes
before execution evict the cached Agent so old tools or system prompts cannot
leak into another mode. Research has coordinator-only tools/skills/identity;
single uses its ordinary configuration and remains capable of doing the work.

`research_create(question, models, budget, baseline?, tasks?)` creates the native
graph. Kanban requires one `{title, body, depends_on?}` task per model, in dependency
order; indices in `depends_on` are zero-based and refer to earlier tasks. Swarm
requires two or more models and forbids preassigned task directions.

## Native automatic dispatch

Both modes automatically release work once the full graph is constructed.
There is **no draft approval gate, manual start API or confirm-start button**.
The root stays in native triage only during construction, then is completed as a
non-execution planning/public-blackboard card. Native task dependencies,
`recompute_ready`, `dispatch_once` and `_default_spawn` own actual release/launch.

Task creation can consume model budget; the lead must explain the actual models,
worker count and budget, and must not tell users that created work is an unstarted,
free draft. Service startup makes no new research task, but resumes eligible native
work, including legacy UI-confirmation drafts. Migration removes only the retired
`Awaiting explicit user confirmation` blocks; real worker input/capability/failure
blocks are preserved.

Native task/run records determine UI state. Workers are ordinary Hermes CLI
processes with independently generated configured execution identities, sessions,
workspaces and optional model/toolsets/MCP settings. Those identities are not a
user-facing profile selector. No Kubernetes worker lifecycle is needed.

## Coordinator and evidence

The lead is conversational but **only coordinates and aggregates**, in both modes.
Skills, per-turn instructions and tool filtering prohibit solving, candidate
implementation, experiments and independent verification by the lead; the tool
restriction survives refresh. Its tools are native read/comment/attachment-list,
research config/create helpers, skill loading, clarification and image input.

Kanban exposes completed deliverables and explicitly published scoped stages;
unpublished worker notes stay private. Both modes use standard native root
blackboard comments for stage publication:

```text
[swarm:blackboard] {"key":"stage:<unique>","value":{
  "scope":"subproblem 1 / lemma A under stated assumptions",
  "claim":"...","evidence":"...","reproduction":"...",
  "limitations":"...","references":["stage:..."]}}
[swarm:blackboard] {"key":"review:<unique>","value":{
  "stage":"stage:...","verdict":"PASS|FAIL|INCONCLUSIVE",
  "evidence":"...","reproduction":"...","limitations":"..."}}
```

When a worker checks independently reproducible subproblem, lemma, boundary or
experiment evidence within its assigned acceptance scope, it must publish that
scoped result **before continuing other exploration**, not accumulate it until the
whole problem ends. Attach public evidence first when needed, then cite it in a
root `stage:` comment: attaching alone or commenting only on the worker card does
not publish a stage. Publication is not `kanban_complete`, does not release native
dependencies, and does not satisfy unfinished acceptance criteria. Workers choose
their own order, without a phase barrier or fixed submission count; a genuinely
joint result can be one stage. `max_turns` is a goal-loop limit, not a stage count.
Do not over-split minimal Kanban deliverables or broadcast unverified guesses.

The public feed displays stage discoveries **and verification reports as separate
published records**. Each record names its Agent/model, native comment ID and time;
reviews identify the target stage and original publisher. Optional `scope` declares
coverage, not correctness or full completion; a review inherits only its target
stage's declared scope. Legacy stages without scope remain valid and are never
retroactively relabeled or split. All historical reports remain visible.
`research_publications` gives the lead cursor-paged, explicitly marked excerpts
(including bounded scope when declared), without private notes or the whole
research prompt. The lead reports checked partial candidates, pending peer reviews
and remaining coverage separately from whole-task completion.

Projection accepts configured worker identities, preserves first stage publication,
uses the latest report per independent peer for consensus and excludes self-review
from that consensus (self/unmatched reports remain visibly marked in the feed).
`PEER_SUPPORTED` means at least two independent PASS reports and no FAIL;
`DISPUTED` means a peer reports FAIL. Neither is a formal proof or trusted VERIFIED
certificate. Native task done/goal judge acceptance also does not prove a theorem.
Automatic Lean checking is not implemented.

Unpublished notes remain private by protocol. Native Kanban's trusted-local-user
model is not malicious-code isolation against arbitrary same-UID filesystem/DB
access. Do not confuse it with an OS sandbox.

## Multi-session Agent board

The **Agent 看板** button lives next to the current chat composer, not in the
global page header. The board belongs to that chat and automatically follows
session navigation; there is no independent session picker. Each session can
have multiple task groups with their original mode. Switching chats never
stops/restarts agents or changes their task assignment. The default view shows **all Agents in the selected session**, grouping
cards by project and sorting active groups first, prioritizing groups with blockers
among those active groups. New groups (including a new GLM
worker) appear automatically. An optional group filter is explicitly a historical
view; the session overview and active-count badge do not disappear. Native child
edges, assignees and run records are authoritative, not the initial worker list.
No stale detail/cancel action from another selected session is retained. Polling
updates clocks without rebuilding unchanged content and preserves expanded details.

The board is a compact workbench: one state cell per actual Agent (not a completion
percentage), horizontal group filters, two-column execution cards on wide screens
and stacked cards on phones. Latest actions and blockers are visible; run diagnostics,
recent tools and delivered results expand on demand. Stage submissions and peer
reports have separate type/verdict labels and retain their sources. Failed updates
keep the previous records with an explicit warning. The panel follows the editor's
size and stays above it instead of covering the message input; Escape closes it.

UI shows actual task/run state, model, PID, OS liveness, elapsed run time, heartbeat
age, native failure limits, timeout reasons and unfinished dependencies. Operations
come from native Hermes tool names/timestamps in messages and bounded operation-log
reads (recent completed tools and model request status), not just optional heartbeat
notes. The last actual action stays visible while the next model request is pending.
Tool arguments, output, private notes and reasoning are never projected. Heartbeat
is labeled as liveness, not an operation report; missing/stale heartbeats are labeled. Public evidence uses the shared sanitized Markdown/formula
renderer. Each group has **停止这一组**, using native reclaim/archive. The board
also shows native model/tool counts and latency totals, and authenticated native
attachment downloads (board-scoped URLs and private browser caching).
There is no navigation link to general.

## Configuration and budget

- `COMPLEX_HOME`: unified session/artifact store and research state, default `~/.complex`.
- `COMPLEX_SINGLE_HOME`: ordinary Hermes tools/MCP/skills/identity configuration;
  falls back to explicit `GENERAL_HOME`, then `COMPLEX_HOME/single`. Local example
  reuses the original general home. Existing configuration is not replaced with
  the coordinator configuration. All modes use the configured model selector.
- `COMPLEX_MODELS_FILE` / `COMPLEX_ENDPOINTS`: configured model registry; see
  `models.example.json`. Use `model_env` / `api_key_env`, never inline credentials.
- Each endpoint may configure worker `toolsets`, native `mcp_servers` and native
  `agent` settings (e.g. `reasoning_effort`). Explicit endpoint settings override
  the root reasoning setting; otherwise provider defaults remain unchanged.
  Task `max_iterations` controls `agent.max_turns`. The lead does not inherit
  execution capabilities.
- `COMPLEX_MAX_WORKERS`: native per-owner-board concurrency limit, default 8.
- `COMPLEX_BASELINES_DIR`: approved baseline source root; each worker gets a copy.
- `MEDIA_USE_S3=0|1`: independent storage flag, unrelated to product/local/auth.
- `WEBUI_LOCAL_MODE=1`: loopback-only without a password. Production uses existing
  `AUTH_JWKS_URL` / gateway authentication; no separate research login.

`wall_seconds` limits each native run; `max_iterations` limits each Hermes turn.
Default Kanban uses native single-shot execution, without a goal judge;
`max_turns` bounds the continuous goal loop only in Swarm. These are not exact
aggregate tokens/dollars. Task scope is its assigned acceptance criteria, not
repeated whole-project validation on every implementation card.

Failure/retry policy follows native Kanban: no per-task `max_retries` override.
Explicit root `kanban.failure_limit` and `kanban.dispatch_stale_timeout_seconds`
are passed to native dispatch; absent settings use its defaults (Hermes 0.19's
failure limit is 2). This is a consecutive-failure threshold, not an additional
retry count: limit 1 blocks after the first failure, limit 2 permits one retry.
Existing genuine blocked/exhausted tasks are NOT rewritten or automatically
unblocked by this fix; another exhausted run needs user authorization. Native
retries retain the task workspace.

Closing the page does not stop workers. Stopping the WebUI stops its dispatcher,
not durable native workers. Native root subscriptions/cursors observe both stage
and verification publications. Child subscriptions use the same native cursors
with the root as `thread_id` to report completion, timeout and blocked/failure
events. `research_status` gives the lead public task/run/action/dependency facts
without private work. These admit a coordinator turn that proactively reports
who found what, who verified what, supporting/contrary evidence and remaining
limits. Notification turns may not solve, verify or spawn more tasks.
After WebUI restart, the originating route is restored only from real Hermes
session ownership/compression lineage and its recorded configured model. Missing
sessions/models keep events pending; no endpoint or recipient is guessed.
Notification admission batches native root/child subscriptions per root. Default
publication/completion events are briefly coalesced; later lifecycle-only notices
report short deltas rather than repeating full deliverables. Swarm stage publication
never waits for worker completion.

## Worker completion, model reassignment and sleep

`worker_runtime.py` uses the native tool-completion callback to interrupt the
worker's model loop only after its own `kanban_complete` succeeds. Rejected
completion and stage/review comments do not stop a Worker. Completion is a serial
boundary: following tools are cancelled by native interrupt handling. Workers
have no `delegate_task` or shared `skill_manage`, and no automatic post-turn
memory/skill review. Explicit work/verification and test-service cleanup happen
before completion, not after the card is already marked done. General is unchanged.

After explicit user authorization, `research_reassign(task_id, model,
user_authorized=true)` creates a configured immutable execution identity and
calls native assign/unblock on the SAME stopped unfinished task. It retains
workspace, root, dependencies, timeout and run history. Historical publications
retain the original execution identity/model. Notification turns cannot recover
a task automatically.

`COMPLEX_PAUSE_SLEEP=1` (default on macOS) uses Python's sleep-excluding monotonic
clock for task elapsed time and Hermes' ephemeral model-stream timers. Wall
timestamps remain real timestamps. Anchors are stored in existing native run
metadata, survive service restarts on the same boot, and freeze at run closure.
The compatibility adapter changes only native timeout's elapsed expression; all
native host checks, signals, transitions and failure policy remain native. It
fails loudly if that installed function changes. `0` keeps native wall-clock
accounting. No system power settings are changed. Existing closed/blocked runs
are not rewritten or resumed. Machine sleep is still real network interruption;
Hermes' existing reconnect logic handles a stale connection after waking.

## Compatibility and images

`native_compat.py` retains a targeted fix for Hermes 0.19's unsafe POSIX post-commit raw database-file
open/close guard, which releases process record locks and can invalidate live WAL
readers. It substitutes `PRAGMA quick_check(1)` on the existing SQLite connection
when the installed source still has that guard. A native `HERMES_BIN` CLI shim
applies the same fix to workers. General is unaffected.

`docker/Dockerfile.webui` shares a runtime base across its targets. The default
`unified` target and explicit `--target complex` provide the three-mode entry and
handwritten capability copy. `--target general` remains standalone and direct-only.
The existing CP pipeline can therefore build the unified service without a target
override or a new pipeline. For local startup, export shell credentials, load
`local.env.example` with `set -a`, then run the command above. No S3 or cluster
is required.

For an existing direct-only deployment, `WEBUI_DATA_HOME` keeps its original
`state.db`, artifacts and input media in place; `COMPLEX_SINGLE_HOME` keeps the
ordinary configuration/skills, while `COMPLEX_HOME` is a separate coordinator/native
board home. No database copy or shared config overwrite is needed.
`COMPLEX_LEGACY_MODE=single` preserves existing chats without research history as
locked single execution; the default `kanban` preserves earlier research-only
installations. Actual native research groups and saved UI choices still take
precedence. Anonymous model endpoints explicitly set their configured
`api_key_env` variable to `none`; missing credential variables are still rejected.
`k8s/webui.yaml` configures the shared PVC paths, online JWKS authentication,
S3 flag, existing local chat endpoints and the validated Ark research endpoints.
