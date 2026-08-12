---
name: weight-cache
description: "Cache NIM model weights on an SPCS block storage volume so they survive suspend/resume, and snapshot a populated cache to seed new services without re-downloading. Includes the measured cases where caching makes startup SLOWER and how to avoid them. Triggers: weight cache, nim cold start, cache model weights, block storage volume nim, fromSnapshot, snapshot weights, seed new service, speed up nim startup, avoid re-downloading weights, scale to zero nim, nim suspend resume."
---

# Cache NIM weights on a block volume

A NIM ships no weights. `NIM_CACHE_PATH` (`/opt/nim/.cache`) does not exist in the
image, so every cold start authenticates to NGC and downloads them. Mounting a
block storage volume there makes the download a one-time cost.

## Decide first: do not do this for speed

**Both shipped NIMs default to `weight_cache: false`.** Turn it on for one specific
reason, described below, which is not latency.

Measured on llama-3.1-8b-instruct 2.0.9, 1x A10G, ~30 GB of weights. All three runs
had the image already on the node and the pool already `IDLE`:

| Run | Config | Downloads | Create → READY |
|---|---|---|---|
| A | no cache | 18 files, 78.2 s download phase | **203 s** |
| B | cache, first fill (80 Gi @ 750 MiB/s) | 18 files | 227 s |
| C | cache, warm resume | **0**, 22 cache hits | **197 s** |

Removing a 78-second download saved **6 seconds**, about 3%. Downloads are not the
bottleneck: NGC pulls 8 files in parallel and moved ~30 GB at roughly **400 MB/s**.
What dominates the remaining two minutes is vLLM engine init — GPU weight load,
`torch.compile` (17.8 s alone), CUDA graph capture, profiling — none of which caching
touches.

The same holds at the small end. On genmol (~500 MB), caching at the **default**
125 MiB/s volume throughput made startup *slower* (18.2 s vs 12.2 s container phase),
because reading a small model off a slow volume costs more than re-fetching it. At
750 MiB/s it came back to 10.7 s.

Storage is not the objection either: an 80 GiB volume is 0.08 TB billed at the storage
rate, against a `GPU_NV_M` pool measured at 2.44 credits/hour. The real cost is
complexity — three immutable volume fields, a snapshot lifecycle, a 100-snapshot
account cap, and a `STOP ALL` footgun.

## The decision, now that egress can be scoped

Caching does get you to **literally zero** egress. Verified: a service created with
`weight_cache_from_snapshot` set and **no `EXTERNAL_ACCESS_INTEGRATIONS` at all**
reached READY and served real inference. `DESCRIBE SERVICE` showed
`external_access_integrations = None`; the log had 0 downloads, 22 cache hits, zero
references to `api.ngc.nvidia.com`, `authn.nvidia.com`, or `nvcr.io`, and zero
connection errors.

That is a narrower advantage than it first appears, because the runtime egress rule
does **not** need `0.0.0.0`. It can be scoped to 7 named vendor hosts (see the setup
skill). Most security reviews will accept named vendor hosts, which means most
customers do not need the cache at all.

| Requirement | Do this |
|---|---|
| Defensible egress | scope the runtime rule to the named hosts. **Do not cache.** |
| **Zero** egress, no external network whatsoever | cache, seed from a snapshot, drop the EAI |
| Faster startup | neither. Caching saved 3%. |

**Caveat on the zero-egress path:** verified for llama-3.1-8b. It will **not** work
as-is for genmol, which fetches a HuggingFace tokenizer (`datamol-io/safe-gpt`) at
model-init time. That download is not part of the NIM weight cache and is not covered
by a snapshot of `NIM_CACHE_PATH`. Per-NIM, verify by creating the seeded service with
no EAI and confirming READY before you promise anyone zero egress.

Two secondary reasons, neither measured:

- **A large download that fails intermittently** becomes a one-time risk instead of a
  per-start one. llama's ~30 GB download succeeded on every attempt here, so treat
  this as unproven.
- **Pinning exact weight bytes.** A normal cold start re-resolves the profile manifest
  from NGC; a seeded snapshot fixes the content, which is what an audit trail needs.

## If you do turn it on

- **Always set `weight_cache_throughput`.** 750 is the ceiling while `iops` stays at
  its 3000 default (AWS allows 1 MiB/s per 4 IOPS). The 125 default produced the
  genmol regression above.
- **Size for the workspace too, not just the weights.** llama materializes its
  workspace *inside* `NIM_CACHE_PATH` (`/opt/nim/.cache/tmp/...`), so the volume needs
  the downloaded weights plus a materialized copy. 80 Gi was right for ~30 GB of
  weights; 60 Gi would have been tight.
- Compare the container-internal phase from the logs, not wall clock, if you are
  measuring. Create-to-READY is dominated by node provisioning and image pull and
  varies run to run.

## Paths & connection (set once)

```bash
PLUGIN_DIR="<absolute path to this plugin>"
WORKDIR="$(mktemp -d)"
CONN="<snowflake connection name>"
CONFIG="$PLUGIN_DIR/config.json"
```

## Step 1: Enable the cache

Per NIM in `config.json`:

```json
"weight_cache": true,
"weight_cache_size_gi": 60,
"weight_cache_path": "/opt/nim/.cache",
"weight_cache_from_snapshot": null,
"weight_cache_throughput": 750,
"weight_cache_snapshot_on_delete": true,
"weight_cache_snapshot_delete_after": "7d"
```

Size it to the weights plus headroom; the volume also carries `lost+found` and the
NIM's own manifest. The renderer validates the range (1-65536 Gi on AWS) and the
throughput ceiling, so a bad value fails at render rather than after a GPU node
has been provisioned.

**These are immutable after creation.** Size, iops, throughput, and encryption
cannot be changed on an existing service, and volumes cannot be added or removed —
changing any of them means dropping and recreating the service. A service with a
block volume also must have `MIN_INSTANCES = MAX_INSTANCES` (the plugin sets both
to 1).

Then re-render and deploy as usual (`/nvidia-nim-on-spcs:nim-deploy`). No `uid`/
`gid` is needed: block volumes mount `root:root` with mode `0777`, and the NIM runs
as uid 1000 (`ubuntu`), so it can write to the mount. The spec reference documents
`uid`/`gid` for stage volumes only, which reads like a problem and is not one.

## Step 2: Confirm the cache is actually being used

The log is the proof, not the timing:

```bash
snow sql -c "$CONN" -q "select SYSTEM\$GET_SERVICE_LOGS('<db>.<schema>.<service>', '0', '<container>', 300);"
```

| First start | Later starts |
|---|---|
| `fetching filemap from: https://api.ngc.nvidia.com/...` | absent |
| `Downloaded filename: model_v2.ckpt to blob: "/opt/nim/.cache/ngc/hub/..."` | `Skipping download, using cached copy of file: model_v2.ckpt` |

If a resume still shows downloads, the volume is not persisting — check that
nothing ran `ALTER COMPUTE POOL ... STOP ALL` (see Step 5).

## Step 3: Snapshot the populated cache

Only needed to seed a *new* service or to survive a drop. **Not** needed for
suspend/resume, which preserves the volume on its own.

Take it when the service is READY and has served at least one request — a snapshot
of a half-downloaded cache seeds a broken service.

```bash
snow sql -f "$WORKDIR/rendered/40_WEIGHT_CACHE_SNAPSHOTS.sql" -c "$CONN" --enable-templating NONE
```

Names are timestamped (`<KEY>_WEIGHTS_<YYYYMMDD_HH24MISS>`) rather than fixed,
because `CREATE OR REPLACE SNAPSHOT` deletes the previous snapshot irrecoverably —
a fixed name would let a re-run destroy the only good copy.

Wait for `CREATED`. A service built against a snapshot still in `INITIALIZED`
fails outright. It took about 2m45s for a 20 Gi volume:

```bash
snow sql -c "$CONN" -q "DESCRIBE SNAPSHOT <db>.<schema>.<snapshot>;"
```

## Step 4: Seed a new service from the snapshot

Set `weight_cache_from_snapshot` to the fully qualified snapshot name, re-render,
and create the service. Verified: a seeded service performs **zero** downloads on
its very first start, and the seeded weights serve valid inference.

The snapshot's encryption type must match the new volume's, and the owner role
needs USAGE on the snapshot and on its database and schema.

To restore a snapshot onto an *existing* service instead, suspend it first —
`ALTER SERVICE ... RESTORE VOLUME "weights" INSTANCES 0 FROM SNAPSHOT <name>`,
which auto-resumes the service. Note this DELETES the current volume contents.

## Step 5: The suspend/resume rule, and the trap

- `ALTER SERVICE ... SUSPEND` and `ALTER COMPUTE POOL ... SUSPEND` **preserve** the
  volume. GPU billing stops, volume billing continues (a small fraction of a GPU
  node), weights survive. This is the cheap idle state.
- `ALTER COMPUTE POOL ... STOP ALL` **deletes** the volume. So does
  `DROP SERVICE ... FORCE` and `ALTER SERVICE ... RESTORE VOLUME`.

With `snapshotOnDelete: true` a deletion first writes
`SYS_BACKUP_ON_DELETE<...>_<timestamp>`, retained per `snapshotDeleteAfter`. Useful,
but they accumulate against the **100-snapshot account limit** and they bill. Prune:

```bash
snow sql -c "$CONN" -q "SHOW SNAPSHOTS IN SCHEMA <db>.<schema>;"
snow sql -c "$CONN" -q "DROP SNAPSHOT <db>.<schema>.<name>;"
```

A dropped snapshot still bills through its retention period (1 day by default).

`snapshotOnDelete` is also a guard rail, not only a cost knob. With it set to
`false`, `DROP SERVICE` **refuses**:

```
... volumes attached to this service are not configured with snapshotOnDelete=true
and are not safe to delete. Please manually snapshot those volumes if needed, and
force delete the service using DROP SERVICE <name> FORCE.
```

So `true` (the plugin default) makes drops frictionless and leaves a billing
snapshot behind; `false` makes an accidental drop fail loudly and requires an
explicit `FORCE`. Pick deliberately: `false` is the better setting for a cache that
took hours to populate.

## Limits worth knowing before designing around this

| Limit | Value |
|---|---|
| Block volumes per service | 3 |
| Block volumes per account | 100 |
| Snapshots per account | 100 |
| Volumes per node | 22 on `GPU_NV_S`, 21 on `GPU_NV_M`, 14 on `GPU_NV_L` |
| Volume size (AWS) | 1 Gi - 65536 Gi |

Exceeding a per-node limit does not error — service instances sit in `PENDING`
waiting for placement.
