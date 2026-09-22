# buda

An inference + retrieval service built **on top of** autumn-rs. It is a *user* of
autumn, not part of it — which is why it lives in its own repository even though
every piece of it talks to an autumn cluster.

```
buda/
├── docker/
│   ├── Dockerfile.freetoken     FreeToken serving image (+ the autumn client)
│   ├── Dockerfile.webui         hermes webui (no autumn client — see below)
│   └── README.freetoken.md      why the image is shaped the way it is
├── webui/                       the webui itself + ARCHITECTURE.md
└── k8s/
    ├── freetoken.yaml           MoE serving on one RTX 4090
    ├── upload-models.yaml       one-shot: load a checkpoint into autumn fs/
    ├── lance-mcp.yaml           LanceDB MCP over an Autumn FUSE mount
    └── webui.yaml               chat + session management
```

## What it does

**Serving** — FreeToken runs a large MoE model on a single consumer GPU by
keeping the routed experts in host RAM and computing part of them on the CPU.
Host memory, not VRAM, sets the model ceiling; the weights become a cold-start
streaming problem rather than a resident one, which is what makes it sensible to
keep them in autumn and read them through a FUSE mount.

**Retrieval** — `lance-mcp` runs unmodified community LanceDB against an Autumn
FUSE path, indexes the document corpus, and exposes the result over MCP/HTTP.

**Chat** — `webui` is a hermes front end, now positioned as **deepwiki**: a
multi-project DeepWiki-style site. The homepage lists project cards; each
project is an `AgentProfile` (its own brief, toolsets, MCP subset and
workspace folder), declared in hermes' config.yaml (`profiles:`) or the
`DEEPWIKI_PROFILES` env. Session management and a chat box otherwise. It
reaches retrieval through `lance-mcp`'s **HTTP** MCP transport rather than
spawning it, so it holds no autumn credential and is the one workload here NOT
bound by the WIRE lockstep below. See `webui/ARCHITECTURE.md`.

Storage-backed workloads mount Autumn FUSE in the privileged app container.
This cluster does not propagate sidecar mounts reliably, so one mount namespace
is intentional. LanceDB reads and writes ordinary paths under that mount.

## What lives here vs in autumn-rs

Here: the FreeToken image, and the manifests for these workloads.

In autumn-rs: the cluster itself (manager / extent-node / partition-server /
etcd / dashboard), the all-roles image and its entrypoint, `autumn-fuse`, and
`memory-mcp`'s source. This repo consumes those as binaries in a published
image — it does not fork or vendor them.

The dividing question is "would this exist if the workload went away?" The
`fuse` entrypoint role would: it is a capability of the storage system. The
Lance MCP source and these manifests would not.

## Dependency on the autumn image

`Dockerfile.freetoken` lifts `autumn-fuse`, `autumnfs` and `autumn-op` out of the
autumn-rs image via a multi-stage `COPY --from`, rather than rebuilding them:
one Rust build, one binary, and it cannot drift from the cluster it dials.

That makes the **WIRE lockstep** rule this repo's problem too. The image embeds
autumn's PyO3 client, and rkyv has no cross-version compatibility — a mismatched
client is refused at the handshake, and worse, a matching wire fingerprint across
a layout drift decodes garbage silently. So `AUTUMN_IMAGE` must name the same
commit the cluster is running:

```
--build-arg AUTUMN_IMAGE=<cr>/autumn-rs:${SCM_COMMIT_ID}
```

with `${SCM_COMMIT_ID}` resolving to a commit whose autumn-rs image **already
exists** — build autumn-rs first, then this. Getting that order wrong fails at
`resolveBaseImage`, which is at least loud.

## Cluster facts these manifests assume

- Namespace `autumn`, so pods can dial manager/PS pod IPs on flat pod networking.
- Node `192.168.3.2` for serving: FreeToken needs driver r580+ (CUDA 13), and it
  is the only node that has it. The other 4090 box is on 550.
- Secret `autumn-credential` with `fs.cred` / `kvc.cred` / `mem.cred` —
  per-family least privilege. autumn protects **every** key once authz is on
  (the partition server's `protected_prefixes` list is retired), so a workload
  without the right credential does not degrade, it is denied.
- `fs/` presplit before any data was written. Splitting a populated partition
  mostly fails on `has_overlap`.

## Building

Built by a Volcengine CP pipeline (workspace `dongmao-lerobot`), from this repo,
Dockerfile `docker/Dockerfile.freetoken`, context the repo root.

```
--build-arg BASE_REGISTRY=hub-cache-cn-beijing.cr.volces.com/
--build-arg APT_MIRROR=https://mirrors.aliyun.com
--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
--build-arg RUSTUP_DIST_SERVER=https://rsproxy.cn
--build-arg RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
--build-arg CARGO_MIRROR=sparse+https://rsproxy.cn/index/
--build-arg AUTUMN_IMAGE=<cr>/autumn-rs:<autumn-commit-sha>
```

Two of those are not optional and not obvious:

`PIP_INDEX_URL` — the default index resolves pypi.org but pulls wheels from
files.pythonhosted.org, which stalls from the CN build pool: the build hangs with
no output rather than failing. Note `freetoken` itself does **not** use this arg
— it has its own `FREETOKEN_INDEX_URL`, because not every mirror carries it (the
ivolces mirror answers `from versions: none` for that one package, which is how
this was found).

`AUTUMN_IMAGE` — see the lockstep note above. Unlike the other args this one is a
correctness constraint, not a speed one.

## Deploying

```bash
kubectl -n autumn apply -f k8s/lance-mcp.yaml
kubectl -n autumn apply -f k8s/upload-model-minimax.yaml
kubectl -n autumn apply -f k8s/freetoken.yaml
```

Fill the `IMAGE_*` placeholders first. Deploy `lance-mcp` before switching the
webui endpoint; its first start builds the persistent LanceDB index and only
becomes ready after that succeeds.

## Status

FreeToken is deployed and reads its weights from an autumn-fuse mount. The
tracked retrieval target is `lance-mcp`; until that image and the updated
webui manifest are rolled out, the live webui remains on `memory-mcp`. The
storage path itself has been verified against the live cluster:

- `O_DIRECT` reads work on an autumn-fuse mount at 4 KiB and 8 MiB — the load
  path this whole design rests on, since FreeToken probes `O_DIRECT` once and
  otherwise falls back to a `MAP_SHARED` mmap that a FUSE `direct_io` file
  refuses outright. `--expert-load parallel` therefore works and is what the
  manifest uses.
- Reads from the mount sustain ~2.4 GB/s at 24-way concurrency (~97 MiB/s
  single-stream — this path scales with concurrency, not with one reader, so a
  serial measurement understates it by an order of magnitude).
