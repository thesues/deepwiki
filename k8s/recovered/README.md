# Recovered manifests

These two workloads had **no manifest in any repo** — they existed only as live
objects on the cluster. During the wire-40 rollout (2026-09-15) each was
recovered with `kubectl get -o yaml`, stripped of runtime-only fields
(status, resourceVersion, uid, defaulted fields), and checked in here so the
next rollout does not have to guess.

- `comfyui-autumn.yaml` — image `comfyui-autumn:e2e71a7d…` (rebuilt against the
  wire-40 freetoken-l3 base), fusebin init `autumn-rs:633a4832…`.
- `autumn-toolbox.yaml` — image `autumn-rs:633a4832…`.

If a manifest for these lands in a proper directory later, delete the copy here.
