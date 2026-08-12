# NVIDIA NIM on Snowpark Container Services

Deploy NVIDIA NIM inference microservices on SPCS **with no local Docker**.

The plugin mirrors NIM images from `nvcr.io` into a Snowflake image repository
using a build job that runs inside the account, then stands up GPU services with
readiness probes, scoped egress, and PAT-authenticated endpoints.

## Why this exists

Getting a NIM onto SPCS the documented way takes 11 manual steps across three
tools, and step one is `docker pull` of a 9-47 GiB image through a laptop. For
regulated customers who cannot install Docker on a managed endpoint, that is a
hard stop rather than an inconvenience.

Measured on `nvcr.io/nim/nvidia/genmol:2.0` — 9.44 GiB compressed, 98 layers:

| | `docker pull` / `tag` / `push` | this plugin |
|---|---|---|
| Local Docker | required | none |
| Bytes through the workstation | 9.44 GiB | 0 |
| Wall clock | tens of minutes | **82 seconds** |
| Egress for the pull | n/a | **4 hosts, scoped** |
| Upstream manifest digest | rewritten by Docker | **preserved** |
| `--platform linux/amd64` footgun | omit it and it fails at runtime | not applicable |

Digest preservation is the load-bearing claim: the mirrored image reported
`sha256:ead01aa98cd5c...`, byte-identical to the upstream amd64 manifest, verified
by re-downloading the largest layer (2.48 GiB) from the Snowflake registry and
recomputing its SHA-256. All 98 blobs are resident. A service created from the
mirrored image reached `READY` with 0 restarts.

## Skills

| Skill | Does |
|---|---|
| `/nvidia-nim-on-spcs:setup` | build pool, one GPU pool per NIM, both egress rules, image repository, build stage, NGC secret |
| `/nvidia-nim-on-spcs:mirror-image` | get images into the account with no Docker; covers both the shipped CLI and the credential-injected job |
| `/nvidia-nim-on-spcs:nim-deploy` | create the GPU services, wait for READY, validate with a real inference call |
| `/nvidia-nim-on-spcs:weight-cache` | cache weights on a block volume so they survive suspend/resume; snapshot and seed |
| `/nvidia-nim-on-spcs:nim-ops` | status, logs, endpoints, suspend/resume for cost, add a NIM, diagnose readiness failures |
| `/nvidia-nim-on-spcs:deploy-all` | all of the above with a gate before each phase that spends |
| `/nvidia-nim-on-spcs:teardown` | stop billing, or remove everything |

Start with `deploy-all` on a fresh account.

## Preflight: the NGC key is validated before anything is created

`assets/preflight/check_ngc_key.py` runs against the live registry before the Snowflake
secret exists:

| Check | Catches |
|---|---|
| `GET api.ngc.nvidia.com/v2/users/me` | expired or mistyped key (401) |
| `proxy_auth` pull scope, per enabled NIM | valid key with no NVAIE entitlement for that model (403) |
| `tags/list`, per enabled NIM | a typo'd or withdrawn tag, before the mirror spends 4 minutes on it |

Entitlement is granted **per model**, so a key that mirrors genmol may have no access to
another NIM at all. An unentitled repo and a wrong repo name both return 403 and are
genuinely indistinguishable — the script says so rather than guessing.

The key is read from `NGC_API_KEY` (never argv, which `ps` exposes), never printed, and
never written to disk by the script.

## Layout

```
.cortex-plugin/plugin.json      plugin manifest
config.json                     per-account, gitignored
assets/config.sample.json       copy this to config.json
assets/build_manifest.yaml      objects, phases, roles, ordering
assets/renderer/render.py       Jinja renderer, StrictUndefined
assets/templates/*.j2           the SQL and the build-context script
skills/<name>/SKILL.md          the six skills above
```

## Configuration

`config.json` carries a NIM catalog. Adding a NIM changes the *contents* of the
rendered files rather than adding new ones, so everything is additive:

```json
{
  "key": "genmol",
  "enabled": true,
  "image": "nvcr.io/nim/nvidia/genmol",
  "tag": "2.0",
  "service_name": "NIM_GENMOL_SVC",
  "gpu_pool": "NIM_GENMOL_POOL",
  "gpu_pool_family": "GPU_NV_S",
  "gpu_count": 1,
  "memory_request": "12Gi",
  "memory_limit": "24Gi",
  "port": 8000,
  "health_path": "/v1/health/ready",
  "invoke_path": "/generate",
  "public_endpoint": true,
  "env": {}
}
```

`env` is passed straight to the container and is not optional decoration. genmol needs
nothing, but llama-3.1-8b on a single A10G will not start without
`"env": {"NIM_MAX_MODEL_LEN": "16384"}`: the A10G leaves 4.2 GiB for KV cache after
15.0 GiB of weights, and the model's default `max_model_len` of 131072 needs 16.0 GiB.
vLLM prints the ceiling for the observed memory
(`estimated maximum model length is 34432`), so the log tells you what to set.

`genmol:2.0` ships enabled; `llama-3.1-8b-instruct` ships disabled. Both are
validated on `GPU_NV_S` / `GPU_NV_M` respectively.

Two values are supplied by the skills rather than stored in config, because they
describe the *account* and not the deployment: `org` and `account`, which form the
registry hostnames. The renderer takes them via `--set`.

## How the image mirror works

`snow spcs service build-image` (SnowCLI 3.16+, experimental, behind
`SNOWFLAKE_CLI_FEATURES_ENABLE_SPCS_BUILD_IMAGE`) is the right tool and this
plugin's job is a faithful copy of what it does. Use the CLI directly for any
**public** base image.

It cannot mirror a **private** vendor image today. The CLI runs a fixed job
service on `/snowflake/images/snowflake_images/sf-image-build:0.0.1` — stock
`moby/buildkit:v0.18.2-rootless` plus a Go wrapper at
`/usr/local/bin/image-builder`. The wrapper's `setupRegistryCredentials` writes
`$HOME/.docker/config.json` with exactly one entry: the destination Snowflake
registry, authenticated as `0auth2accesstoken` with the token from
`/snowflake/session/token`. There is no flag and no environment variable for a
source credential, so a private base fails at metadata resolution with
`403 Forbidden` from `nvcr.io/proxy_auth`.

BuildKit reads registry auth from `$DOCKER_CONFIG/config.json`, and a job
specification can set env vars and mount secrets. So `10_MIRROR_JOBS.sql` runs the
same builder binary with a `DOCKER_CONFIG` built at container start holding
credentials for both registries, the source key arriving as a mounted Snowflake
secret. Every executable line of that spec is byte-identical to the one that
produced the verified mirror.

**Delete that template and use the CLI** once `build-image` grows a
source-registry credential option. Nothing else in the plugin depends on the
workaround.

## Weight caching: off by default, and here is why

A NIM ships no weights — `NIM_CACHE_PATH` (`/opt/nim/.cache`) does not exist in the
image, so every cold start downloads them. `weight_cache: true` mounts a block storage
volume there. **Both shipped NIMs default to `false`, and not for cost reasons.**

Measured on llama-3.1-8b-instruct (1x A10G, ~30 GB of weights), identical conditions:

| Run | Config | Downloads | Create → READY |
|---|---|---|---|
| A | no cache | 18 files, 78.2 s download phase | **203 s** |
| B | cache, first fill (80 Gi @ 750 MiB/s) | 18 files | 227 s |
| C | cache, warm resume | **0**, 22 cache hits | **197 s** |

Removing a 78-second download saved **6 seconds**. Downloads are not the bottleneck:
NGC pulls 8 files in parallel at roughly **400 MB/s**. vLLM engine init dominates —
GPU weight load, `torch.compile` (17.8 s alone), CUDA graph capture — and caching does
not touch any of it. On genmol (~500 MB), caching at the *default* 125 MiB/s volume
throughput made startup measurably **slower**.

Storage is not the objection either: 80 GiB is 0.08 TB at the storage rate against a
`GPU_NV_M` pool measured at 2.44 credits/hour. The cost is complexity — three
immutable volume fields, a snapshot lifecycle, a 100-snapshot account cap, and a
`STOP ALL` footgun.

**When to enable it anyway.** A service seeded from a snapshot with **no
`EXTERNAL_ACCESS_INTEGRATIONS` at all** reached READY and served inference — 0
downloads, zero contact with NGC, `external_access_integrations = None`. So caching
gets you to *literally zero* egress.

But that is now a narrower advantage than it looks, because the runtime rule can be
scoped to 7 named vendor hosts (see below) instead of `0.0.0.0`. Most security reviews
will accept named vendor hosts. The decision is therefore:

| Requirement | Answer |
|---|---|
| Defensible egress | scope the rule to the 7 hosts. Do not cache. |
| **Zero** egress, no external network at all | cache + seed from a snapshot, drop the EAI |
| Faster startup | neither. Caching saves ~3%. |

Caveat: the zero-egress path is verified for llama. It will **not** work as-is for
genmol, which fetches a HuggingFace tokenizer at model-init that the NIM weight cache
does not cover. Per-NIM, verify before promising it.

Secondary, unmeasured: caching makes an intermittently-failing large download a
one-time risk, and pins exact weight bytes for an audit trail.

Independently, suspend/resume preserves the volume (Run C: 0 downloads), so idling is
cheap — though as the table shows, the resume is not meaningfully faster. The trap is
that `ALTER COMPUTE POOL ... STOP ALL` **deletes** block volumes (so do
`DROP SERVICE ... FORCE` and `ALTER SERVICE ... RESTORE VOLUME`) — use `SUSPEND`.

## Two egress rules, both scoped

They are separate because they cover different phases, not different risk levels.
Neither needs `0.0.0.0` any more.

**Image pull** — 4 hosts: `nvcr.io`, `layers.nvcr.io`, `authn.nvidia.com`,
`api.ngc.nvidia.com`. `layers.nvcr.io` is the blob host registry responses redirect
to; omit it and the mirror fails as `dial tcp: lookup layers.nvcr.io ... no such
host`.

**Runtime weight download** — 7 hosts, derived empirically and verified:

```
api.ngc.nvidia.com:443      the file manifest
xfiles.ngc.nvidia.com:443   every weight blob, HTTP 200, served directly
authn.nvidia.com:443        token exchange
huggingface.co:443          genmol's tokenizer (datamol-io/safe-gpt)
cdn-lfs.hf.co:443           HF LFS redirect targets, precautionary
cdn-lfs-us-1.hf.co:443
transfer.xethub.hf.co:443
```

An earlier version of this plugin defaulted to `0.0.0.0:443` on the belief that NGC
weight downloads redirect to unpredictable CDN hosts. That was wrong. The NGC files
API returns URLs on one stable host and serves them with no onward redirect —
confirmed against both genmol and llama-3.1-8b, whose manifests contain 3 and 18 URLs
respectively, all on `xfiles.ngc.nvidia.com`.

The genuinely surprising part is that **some NIMs reach non-NVIDIA hosts**. genmol
fetches its tokenizer from HuggingFace at model-init time — unrelated to NGC, and not
covered by any weight cache. llama does not. So this list is a union across the
shipped catalog and may be incomplete for a NIM you add yourself.

The discovery loop is reliable because a blocked host names itself in the log:

```
Failed to resolve 'huggingface.co' ([Errno -2] Name or service not known)
dial tcp: lookup <host> on 169.254.20.10:53: no such host
```

Deploy, read the log, add the host it names, repeat. Do not widen to `0.0.0.0`.

Watch out that a blocked host can surface as something that looks unrelated — genmol
turned a DNS failure into `TypeError: stat: path should be string, bytes,
os.PathLike or integer, not NoneType` several frames into a tokenizer library.

`CHECKS.sql` reports `WARN` on either rule if it still contains `0.0.0.0`.

## Known gaps

Not defects in the plugin — current platform limits worth stating to a customer.

- **No Model Registry integration.** A NIM service is an anonymous container: no
  version history, no lineage, no Models UI, no ML Observability, no inference
  logging. Record the mirrored digest at deploy time if you need an audit trail.
- **No scale-to-zero.** An idle NIM costs the same as a busy one. Suspending the
  pool is the only lever. With a weight cache the resume is cheap, which makes that
  lever practical rather than painful.
- **Cold start re-downloads weights** unless a weight cache is configured. See the
  weight-caching section above — the primitive works, but it is opt-in per NIM and
  it is not a win for small models.
- **Opaque failures.** Image pull failure, blocked weight download, out of disk,
  OOM, missing GPU profile, and entitlement rejection all present as
  `Readiness probe is failing`.
- **93 GiB node storage cap** on every instance family — the real ceiling on how
  large an image can be mirrored.
- **No SQL-native invocation.** A NIM's payload does not match the dataframe
  protocol Snowflake's model services use. Every NIM publishes OpenAPI at `/docs`;
  nothing consumes it to generate typed service functions.

## Prerequisites

- `snow` CLI with a named connection; `ACCOUNTADMIN` for the account phase
- `uv` locally (the renderer needs `jinja2` + `pyyaml`)
- An NGC API key with NVAIE entitlement for the NIMs being deployed
- GPU capacity in the region for each `gpu_pool_family`
