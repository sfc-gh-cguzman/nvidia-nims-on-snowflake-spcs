# Getting a NIM onto SPCS without Docker: two mechanisms, measured

**Author:** Carlos Guzman, Sr. Solution Engineer
**Date:** 2026-08-11
**Status:** Internal working document
**Evidence base:** Hands-on work in a personal AWS us-west-2 sandbox against
`nvcr.io/nim/nvidia/genmol:2.0`. Every number below was measured, not estimated.
Companion to `nim-on-spcs-friction-and-recommendations.md` (2026-07-31), which this
document corrects in two places.

---

## Summary

Two of the frictions in the July document have working mechanisms today, and both
are worth knowing precisely because both come with a catch that is not documented.

**Image movement no longer needs a local Docker.** A 9.44 GiB NIM image can be
mirrored from `nvcr.io` into a Snowflake image repository in 82 seconds, with zero
bytes crossing a workstation, and the mirrored image keeps the upstream manifest
digest. The shipped command cannot do it, because it has no input for source
registry credentials, but the builder it runs will accept them through
`DOCKER_CONFIG`. That is a one-field product gap, not an architectural one.

**Neither phase needs unrestricted egress. Friction #2 is resolved.** An image pull
needs 4 hosts. The runtime weight download needs 7, and a genmol service reached READY
and downloaded weights with that scoped rule and no `0.0.0.0` anywhere. The July
document's claim that NGC weight downloads redirect to unpredictable CDN hosts was
wrong: the NGC files API returns blob URLs on a single stable host,
`xfiles.ngc.nvidia.com`, and serves them with HTTP 200 and no onward redirect. The
non-obvious part is that some NIMs also reach non-NVIDIA hosts - genmol pulls a
tokenizer from HuggingFace at model-init.

**Weight caching on a block volume is not a performance feature, and the measurement
is unambiguous.** On llama-3.1-8b (~30 GB of weights) the cache removed a 78-second
download and saved **6 seconds** of a 203-second startup. Downloads are fast - NGC
moved 30 GB at about 400 MB/s - and what dominates startup is vLLM engine
initialisation, which caching does not touch. Both NIMs in the plugin ship with the
cache off.

**The one thing caching does buy is verified, and it is not speed.** A service seeded
from a snapshot with **no external access integration at all** reached READY and
served inference, with zero downloads and zero contact with NGC. That removes the
open `0.0.0.0:443` runtime rule entirely, which is the only part of this architecture
that would not survive a security review.

**A fatal model-load failure reported itself as `DONE / "Completed successfully"`
with exit code 0.** Worse than the "readiness probe is failing" opacity in the July
document, because it looks like success to anything monitoring service status.

---

## Part 1: Mirroring a vendor image with no local Docker

### What the shipped command actually runs

`snow spcs service build-image` (SnowCLI 3.16+, experimental, behind
`SNOWFLAKE_CLI_FEATURES_ENABLE_SPCS_BUILD_IMAGE=true`) runs a job service on
`/snowflake/images/snowflake_images/sf-image-build:0.0.1`.

That image is stock `moby/buildkit:v0.18.2-rootless` (Alpine, uid 65532
`buildkit`, `buildctl-daemonless.sh`) plus one Go binary at
`/usr/local/bin/image-builder`. Reading its strings gives the whole contract: it
consumes `IMAGE_REGISTRY_URL`, `IMAGE_NAME`, `IMAGE_TAG`, `BUILD_CONTEXT`,
`PUSH_IMAGE`, `TOKEN_FILE_TIMEOUT`, and its `setupRegistryCredentials` writes
`$HOME/.docker/config.json` with exactly one entry: the destination Snowflake
registry, authenticated as `0auth2accesstoken` with the token from
`/snowflake/session/token`. It then shells out to:

```
buildctl-daemonless.sh build --frontend=dockerfile.v0 \
  --local=context=/app --local=dockerfile=/app \
  --output=type=image,name=<dest>,push=true \
  --export-cache=type=local,dest=/home/buildkit/.cache/buildkit \
  --import-cache=type=local,src=/home/buildkit/.cache/buildkit
```

There is no flag and no environment variable for a **source** registry credential.
So a Dockerfile whose base image is private fails before anything is built:

```
#2 ERROR: failed to authorize: failed to fetch anonymous token:
   GET https://nvcr.io/proxy_auth?scope=repository:nim/nvidia/genmol:pull
   -> 403 Forbidden
```

Every NIM is private, so the shipped command cannot mirror any of them.

### The workaround, and why it is small

BuildKit resolves registry auth from `$DOCKER_CONFIG/config.json`, and a job
specification can set environment variables and mount Snowflake secrets. So run the
same builder binary from a hand-written `EXECUTE JOB SERVICE` with
`DOCKER_CONFIG` pointed at a config written at container start that carries both
registries. Because `DOCKER_CONFIG` overrides the wrapper's own file, that config
has to include the destination credential too, rebuilt from
`/snowflake/session/token` with user `0auth2accesstoken`.

The source key arrives as a mounted `GENERIC_STRING` secret, so it never lands in
the Dockerfile, on the stage, in the SQL text, or in shell history.

The build context is a one-line Dockerfile:

```dockerfile
FROM nvcr.io/nim/nvidia/genmol:2.0
```

Do **not** pin `--platform`. SPCS build nodes are linux/amd64, so BuildKit selects
the amd64 entry of a multi-arch manifest on its own. This removes the standard
local-Docker footgun where omitting `--platform linux/amd64` yields an arm64 image
that fails at runtime on the pool rather than at build time.

### Measurements

| | `docker pull` / `tag` / `push` | in-account build job |
|---|---|---|
| Local Docker | required | none |
| Bytes through the workstation | 9.44 GiB | 0 |
| Wall clock | tens of minutes | **82 s** (first run), 3m57s (a later run on a cold node) |
| Egress needed | n/a | **4 hosts, scoped** |
| Upstream manifest digest | rewritten | **preserved** |

`genmol:2.0` amd64 is 9.44 GiB compressed across 98 layers. The push phase reported
8.9 s, which works out to roughly 1 GB/s: blobs stream registry-to-registry inside
the region and never touch a laptop. Because a `FROM`-only Dockerfile has no further
instructions, BuildKit copies resolved layers through without materializing a
filesystem, which is why it is this fast.

### Digest preservation, and how to verify it properly

The mirrored image reported the same manifest digest as the upstream amd64
manifest, `sha256:ead01aa98cd5c...`. The June `docker pull` / `push` of the same
image produced `sha256:6ecbbe94...`, because Docker rewrites the manifest on push.

A fast push invites suspicion that blobs were referenced rather than transferred, so
verify rather than trust. Get a registry bearer token (`/v2/` returns 401 with a
realm at `https://<host>/login`; Basic auth with user `0sessiontoken` and the full
JSON from `snow spcs image-registry token` as the password), then `HEAD` every blob
and re-download the largest to recompute its hash. Result: **98/98 blobs resident,
9.44 GiB**, and the largest layer (2,664,311,156 bytes) hashed to its expected
digest. A service created from the mirrored image reached READY with 0 restarts and
served real inference.

### Egress for an image pull is enumerable

This is the correction to friction #2 in the July document. An image pull resolves a
fixed host set:

```
nvcr.io:443
layers.nvcr.io:443
authn.nvidia.com:443
api.ngc.nvidia.com:443
```

Verified sufficient. `layers.nvcr.io` is the blob host that registry responses
redirect to, and omitting it fails cleanly and diagnosably as
`dial tcp: lookup layers.nvcr.io ... no such host` rather than hanging.

The July document's conclusion that NGC requires unrestricted egress holds only for
the **runtime weight download**, which redirects to CDN hosts that are not
predictable. Separating the two is the practical answer: mirror the image through a
scoped rule, and treat the runtime rule as the open question it is.

### A CLI bug worth filing

`snow spcs service build-image` does not qualify the job service name it generates.
Even with a fully qualified `--stage` and `--image-repository`, it fails with:

```
090105 (22000): Cannot perform EXECUTE JOB SERVICE.
This session does not have a current database.
```

Workaround: pass `--database` and `--schema` explicitly.

### Asks

1. **A source-registry credential option on `build-image`.** Either honour
   `DOCKER_CONFIG`, or add `--source-registry-secret <snowflake secret>`. This is
   the single highest-leverage item, and everything above becomes deletable
   workaround code the day it ships.
2. **Publish the NGC pull host list**, the way the HuggingFace host list is
   published. Four hosts, and the blob host is the non-obvious one.
3. **Qualify the generated job name** in the CLI.
4. **Document the 93 GiB node storage cap** as the ceiling on mirrorable image
   size. Every instance family reports `storage_gib = 93.13`, so this is not
   something you size around by picking a bigger node.

---

## Part 2: Caching model weights on a block storage volume

### The permission question, answered

A NIM does not ship weights. `NIM_CACHE_PATH` is `/opt/nim/.cache` and that
directory does not exist in the image, confirming the July document's claim that the
34-47 GiB is CUDA and PyTorch runtime.

The container runs as **uid 1000 (`ubuntu`)**, non-root. Block storage volumes mount
root-owned, and the specification reference documents `uid` / `gid` for stage volumes
only, which reads like a blocker. It is not: a mounted block volume comes up
`root:root` with mode **0777**, so a non-root container can write to it. Verified by
mounting one into the genmol image on a CPU job and writing to it as uid 1000. No
`uid` / `gid` needed.

Worth documenting in the block storage page, because the natural reading of the
current text is that non-root containers need a field that block volumes do not
support.

### Measurements

genmol 2.0, roughly 500 MB of weights across three files. Timings are the
container-internal phase from proxy-detect to "Application is ready to receive API
requests", taken from the service logs. End-to-end `CREATE SERVICE` to READY is not
comparable across runs because node provisioning and image pull dominate and vary
(5m32s versus 58s for the same work).

| Case | Volume throughput | Downloads | Materialize workspace | Phase total |
|---|---|---|---|---|
| First fill, cache empty | 125 MiB/s (default) | 3 | 2.2 s | **12.2 s** |
| Cache hit after suspend/resume | 125 MiB/s (default) | 0 | 9.7 s | **18.2 s** |
| Cache hit, volume seeded from snapshot | 750 MiB/s | 0 | 2.3 s | **10.7 s** |

The cache worked exactly as intended in both hit cases. The logs are unambiguous:

```
first fill:  Downloaded filename: model_v2.ckpt to blob: "/opt/nim/.cache/ngc/hub/..."
cache hit:   Skipping download, using cached copy of file: model_v2.ckpt
```

Zero `Downloaded filename` lines and zero `fetching filemap` lines on a warm start.

### The finding: caching made it slower

Row 2 is the interesting one. Eliminating a 3.6 s download made the startup 6 s
**slower**, because the NIM copies weights out of the cache into
`/opt/nim/workspace` and that read came off a volume provisioned at the default
125 MiB/s. On the first fill the same files were still in page cache from having
just been written, so the copy was nearly free.

Raising throughput to 750 MiB/s brought materialize back to 2.3 s and made the
cached start the fastest of the three. 750 is the ceiling while `iops` stays at its
3000 default, because AWS allows 1 MiB/s per 4 IOPS.

### Sizing the actual trade

**Measured on llama-3.1-8b-instruct 2.0.9** (1x A10G on `GPU_NV_M`, ~30 GB of
weights: 4 safetensors shards plus `original/consolidated.00.pth`). All three runs
had the image already resident on the node and the pool already `IDLE`, so
create-to-READY is a fair comparison between them:

| Run | Config | Downloads | Create -> READY |
|---|---|---|---|
| A | no cache | 18 files, **78.2 s** download phase | **203 s** |
| B | cache, first fill (80 Gi @ 750 MiB/s) | 18 files | 227 s |
| C | cache, warm resume | **0**, 22 cache hits | **197 s** |

Eliminating a 78-second download saved **6 seconds**, about 3% of startup. The
volume read consumed almost exactly what the download had cost.

The reason is that the download is fast and the rest of startup is not. NGC pulls 8
files in parallel and moved roughly 30 GB in 78 seconds, about **400 MB/s**. What
dominates the remaining ~120 seconds is vLLM engine initialisation: loading weights
onto the GPU, `torch.compile` (17.8 s by itself), CUDA graph capture, and profiling.
None of that is affected by where the weights came from.

An earlier draft of this document extrapolated from genmol at 140 MB/s and predicted
a roughly 90-second saving for a 16 GB model. That was wrong in both directions: the
download is faster than assumed, and the cached path is slower than assumed. The
measurement replaces the estimate.

The storage side of the trade is close to noise. An 80 GiB volume is 0.08 TB, billed
at the storage rate rather than in credits, against a `GPU_NV_M` pool measured at
**2.44 credits/hour** in this account. What caching actually costs is complexity:
three immutable volume fields, a snapshot lifecycle with a 100-snapshot account cap,
and the `STOP ALL` behaviour below.

**Conclusion: do not cache for speed, at any model size we have measured.** Both
NIMs in the plugin default to `weight_cache: false`.

### What the cache buys, and it is not speed

Only one of these survived measurement as a first-order reason, and it has nothing to
do with latency.

1. **It removes the runtime egress requirement. VERIFIED.** A service created with
   `blockConfig.initialContents.fromSnapshot` and **no
   `EXTERNAL_ACCESS_INTEGRATIONS` at all** reached READY and served inference.
   `DESCRIBE SERVICE` confirms `external_access_integrations = None`. The container
   log shows 0 downloads, 22 cache hits, zero references to `api.ngc.nvidia.com`,
   `authn.nvidia.com`, or `nvcr.io`, and zero connection errors. The NIM does not
   phone home to resolve its profile manifest when the cache is populated.

   This is the answer to friction #2 in the July document that does not require
   asking a customer to accept `0.0.0.0:443`. Mirror the image, seed the weights, and
   the NIM runs with no external network access whatsoever. For a regulated account
   that is the difference between a defensible architecture and one that does not
   pass review.

2. **It converts a repeating failure into a one-time one.** The July document records
   four BioNeMo NIMs crash-looping *on weight download* at 20-50 GB. At that size the
   question is not how long the download takes, it is whether it completes on every
   start. Not measured here, since the llama download succeeded on all attempts.

3. **It pins exact weight bytes.** A cold start re-resolves the profile manifest from
   NGC. A seeded snapshot fixes the content, which is what the "which model version
   produced this output" question needs. Follows from the verification in point 1.

Separately: **suspend and resume preserve the volume.** Snowflake reattaches each
volume to the same instance ID, so a resume does not re-download (Run C: 0 downloads).
Volume billing continues while suspended, at a small fraction of a GPU node. That is
a cheap idle state, though as the numbers show, the resume is not meaningfully faster
than a cold start.

### Snapshots
`CREATE SNAPSHOT ... FROM SERVICE ... VOLUME "weights" INSTANCE 0` works as
documented. Volume names are case-sensitive and must be double-quoted.

- A snapshot of a 20 Gi volume took about **2m45s** to move from `INITIALIZED` to
  `CREATED`. A service built against a snapshot that has not reached `CREATED`
  fails outright, so this wait is load-bearing in any automation.
- A service created with `blockConfig.initialContents.fromSnapshot` performed
  **zero** downloads on its very first start, and the seeded weights produced valid
  inference. Seeding works.
- Use timestamped snapshot names. `CREATE OR REPLACE SNAPSHOT` deletes the previous
  snapshot irrecoverably, so a fixed name plus `OR REPLACE` means one re-run against
  a half-populated cache destroys the only good copy.

### The trap

These three commands **delete** a block volume:

- `DROP SERVICE <name> FORCE`
- `ALTER COMPUTE POOL <name> STOP ALL`
- `ALTER SERVICE <name> RESTORE VOLUME ... FROM SNAPSHOT`

`ALTER COMPUTE POOL ... STOP ALL` is the one that will catch people, because it is
the natural thing to reach for when stopping spend and it reads as less destructive
than a drop. `SUSPEND` preserves; `STOP ALL` destroys.

`snapshotOnDelete` defaults to true for services, so a deletion first writes
`SYS_BACKUP_ON_DELETE<...>_<timestamp>`, retained for 7 days by default. Observed
in practice: dropping one test service produced one automatically. Useful, and also
an accumulating cost against a **100-snapshot account limit**. A dropped snapshot
keeps billing through its retention period. Any teardown that claims to be complete
needs to account for these.

It is also a guard rail. With `snapshotOnDelete: false`, `DROP SERVICE` refuses:

```
... volumes attached to this service are not configured with snapshotOnDelete=true
and are not safe to delete. Please manually snapshot those volumes if needed, and
force delete the service using DROP SERVICE <name> FORCE.
```

So the default trades safety for tidiness: `true` lets a drop succeed and leaves a
billing snapshot, `false` makes an accidental drop fail loudly. For a cache that
took a long time to populate, `false` is the better setting, and that is a choice
worth surfacing rather than defaulting silently.

### Asks

1. **A managed weight cache, one account-level snapshot per NIM version**, seeded
   automatically and shared across services. Every primitive exists; nothing
   composes them. This was proposed on SNOW-1417053 in May 2024 in the context of
   the NIM partnership launch and is still something each customer discovers alone.
2. **Set a sensible default throughput, or warn.** A 125 MiB/s default on a volume
   whose documented purpose includes model weights produces a measurable regression.
3. **Document the 0777 mount mode** on the block storage page so non-root
   containers are not assumed to be blocked.
4. **Make `STOP ALL` versus `SUSPEND` louder** in the block volume documentation and
   ideally in the command output.

---

---

## Part 3: Scoping the runtime egress rule

This is the most useful result in either session, and it retires the July document's
most damaging claim.

### The claim that was wrong

The July document concluded that a correctly-scoped NGC network rule "fails", because
"the actual weight download redirects to CDN hosts that are not in the allowlist", and
that the only thing that worked was `0.0.0.0:443` plus `0.0.0.0:80`.

The redirect target is not unpredictable. Querying the NGC files API directly, with the
`nvapi-` key as a plain bearer token:

```
GET https://api.ngc.nvidia.com/v2/org/nim/team/nvidia/models/genmol/2.0.0/files
Authorization: Bearer nvapi-...
```

returns a `urls` array. For genmol that is 3 URLs; for llama-3.1-8b it is 18. **Every
URL in both, on one host: `xfiles.ngc.nvidia.com`.** Fetching one with redirects
disabled returns HTTP 200 directly - `Server: AmazonS3`, so it is S3 behind a stable
CNAME rather than a redirect to a rotating CDN name.

The original scoped rule in the July document listed `nvcr.io`, `authn.nvidia.com`,
`helm.ngc.nvidia.com`, and `api.ngc.nvidia.com`. It failed because it was missing
`xfiles.ngc.nvidia.com`, not because the host was unknowable.

### The part nobody would have guessed

A scoped rule with the three NGC hosts got genmol past the weight download and then
failed at model init:

```
Failed to resolve 'huggingface.co' ([Errno -2] Name or service not known)
  ... while requesting HEAD https://huggingface.co/datamol-io/safe-gpt/resolve/main/tokenizer.json
```

genmol loads its tokenizer from HuggingFace at model-init time. That has nothing to do
with NGC, is not in any NVIDIA-published host list, and is **not covered by the NIM
weight cache** - so it is also the reason the zero-egress seeded-cache path works for
llama but will not work as-is for genmol.

That failure was also badly reported. The DNS error was retried 5 times and then
surfaced as:

```
TypeError: stat: path should be string, bytes, os.PathLike or integer, not NoneType
```

several frames deep in `safe/tokenizer.py`, because the failed fetch returned `None`
into an `os.path.isfile()` call. Nothing in that exception says "network".

### The verified list

```
api.ngc.nvidia.com:443       file manifest
xfiles.ngc.nvidia.com:443    every weight blob, HTTP 200, no onward redirect
authn.nvidia.com:443         token exchange
huggingface.co:443           genmol tokenizer (datamol-io/safe-gpt)
cdn-lfs.hf.co:443            HF LFS redirect targets, precautionary
cdn-lfs-us-1.hf.co:443
transfer.xethub.hf.co:443
```

genmol on this rule: READY, 3 fresh weight downloads, 1 filemap fetch, **0 DNS
failures, 0 HuggingFace retries**. No `0.0.0.0` anywhere. This is now the plugin
default.

Only `huggingface.co` was observed in use; the three HF CDN entries are precautionary
because HuggingFace redirects LFS files to them and they appear in HuggingFace's own
published host list.

### The method, since there is no per-NIM dependency manifest

The list above is a union across two NIMs and is not guaranteed complete for a third.
But discovery is reliable, because a blocked host names itself:

```
Failed to resolve '<host>' ([Errno -2] Name or service not known)
dial tcp: lookup <host> on 169.254.20.10:53: no such host
```

Deploy with the known list, read the container log, add the host it names, repeat.
Note that a *successful* run does not log its download hosts - only the manifest
endpoint appears - so a blocked run is the only way to enumerate them from logs.

### Asks

1. **Publish the NIM runtime host list**, the way the HuggingFace host list is
   published. Four NVIDIA hosts across image pull and weight download, plus whatever a
   given NIM reaches on its own.
2. **Publish per-NIM external dependencies.** genmol reaching HuggingFace is invisible
   until it fails, and it is not something a customer can infer from NVIDIA's model
   card. This is the same gap as the missing NIM-to-GPU-profile manifest.
3. **Correct the internal guidance** that unrestricted egress is required for NIMs on
   SPCS. It is not, and the belief is costing deals in regulated accounts.

---

## Part 4: Two findings from the llama run that are not about caching

### A fatal model-load error was reported as success

This is the most serious diagnostic problem observed in either session, and it is
worse than the "readiness probe is failing" complaint in the July document.

llama-3.1-8b was deployed on 1x A10G without `NIM_MAX_MODEL_LEN`. vLLM computed:
A10G usable 22.06 GiB, weights 15.0 GiB, leaving 4.2 GiB for KV cache. The model's
default `max_model_len` of 131072 requires 16.0 GiB of KV cache for a single request,
so `EngineCore failed to start` with a `ValueError`. The container then exited, and
after five restarts the service reported:

```
status: DONE      restartCount: 5      lastExitCode: 0
message: Completed successfully
```

Exit code 0. "Completed successfully". A service that cannot serve a single request
presented as a completed job. Anything monitoring service status rather than parsing
container logs would record this as a success.

The root cause was in the log and was actionable - vLLM even printed the ceiling,
`estimated maximum model length is 34432` - but nothing in the service-level status
pointed there.

**Ask:** a container that exits after a failed engine initialisation should not
surface as `DONE` with exit code 0. If the NIM's own wrapper is swallowing the
non-zero exit, that is worth raising with NVIDIA through the partnership; if SPCS is
normalising it, that is ours.

### GPU sizing needs per-NIM environment variables, not just resource requests

The fix is one environment variable: `NIM_MAX_MODEL_LEN: "16384"`, comfortably under
the 34432 ceiling. With it, the same service reached READY in 203 seconds.

This is friction #4 in the July document ("resource sizing is guesswork, and the
wrong answer costs a crash-loop") with a concrete instance: the correct GPU family
was chosen, one GPU was correctly requested, memory requests and limits were
generous, and the service still could not start, because the constraint was KV cache
headroom rather than any field expressible in `resources`.

The June deployment in this repository had `NIM_MAX_MODEL_LEN: "4096"` in its
specification. That value carried the knowledge; the plugin's first draft dropped it
by only emitting a fixed set of environment variables. Fixed by adding a per-NIM
`env` passthrough.

A published NIM-to-GPU-profile manifest would have prevented this, and it remains the
P1 ask from the July document. Until it exists, the practical rule is: read the
vLLM memory report in the container log, take the `estimated maximum model length`,
and set `NIM_MAX_MODEL_LEN` below it.

---

---

## Corrections to the 2026-07-31 document

| Earlier claim | Correction |
|---|---|
| NGC egress cannot be scoped; the working configuration is `0.0.0.0:443` | Retracted for both phases. Image pull needs 4 hosts. Runtime weight download needs 7, verified with a genmol service reaching READY and downloading weights, 0 DNS failures, no `0.0.0.0`. The blob host is `xfiles.ngc.nvidia.com`, stable and non-redirecting; the original scoped rule failed because it omitted that host. See Part 3. |
| Image movement requires a local Docker workstation | No longer true. `build-image` exists, and for a private registry the same builder works with `DOCKER_CONFIG` injected. 82 s, no laptop, digest preserved. |
| Weight caching via block volumes would turn a cold start into a warm mount | Wrong as a performance claim. Measured on llama-3.1-8b, caching removed a 78 s download and saved 6 s of a 203 s startup, because downloads run at ~400 MB/s and vLLM engine init dominates. A seeded volume does let a NIM run with NO external access integration (verified on llama), but that is a narrower win now that the runtime rule can be scoped to 7 named hosts - see Part 3. Both NIMs ship with caching off. |
| Weight download is the direct cause of the BioNeMo crash-loops | Still plausible and still unverified. llama's ~30 GB download succeeded on every attempt at ~400 MB/s, so a download being slow is not itself a failure mode. If 20-50 GB NIMs fail, the cause is more likely disk capacity or the engine-init memory class of failure documented in Part 4 than download duration. |
| Weight download is the bottleneck worth engineering around | It is not. On llama it was 78 s of a 203 s startup and NGC served it at ~400 MB/s. vLLM engine init dominates. The recommendation is to just download. |
| NIM external dependencies are all NVIDIA hosts | genmol fetches its tokenizer from `huggingface.co` at model-init, unrelated to NGC and not covered by the NIM weight cache. Per-NIM dependencies are undocumented and must be discovered from blocked-run logs. |

Everything else in that document still holds, including the absence of Model
Registry integration, opaque readiness failures, unstable endpoint URLs, and the
missing SQL surface generated from each NIM's OpenAPI spec.

---

## Where the working implementation lives

`plugins/nvidia_nim_on_spcs/` in this repository, on branch
`feature/plugin-nvidia-nim-on-spcs`. Config-driven NIM catalog, six skills, and
templates that encode each finding above at the point where it matters:

- `assets/templates/10_mirror_jobs.sql.j2` - the credential-injected mirror job,
  with a header saying to delete it once the CLI gains a source-credential flag
- `assets/templates/30_nim_services.sql.j2` - the optional block-volume cache and
  the measurement table that justifies the defaults
- `assets/templates/40_weight_cache_snapshots.sql.j2` - timestamped snapshots
- `skills/weight-cache/SKILL.md` - the decision of whether to cache at all
