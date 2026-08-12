# NVIDIA NIM on Snowpark Container Services: Friction Analysis and Recommendations

**Author:** Carlos Guzman, Sr. Solution Engineer
**Date:** 2026-07-31
**Status:** Internal working document
**Evidence base:** Hands-on deployment of GenMol 2.0 and Llama 3.1 8B NIMs to SPCS (`nvidia-nims-spcs` repo, June 2026)

---

## Summary

Getting a NIM running on SPCS took 11 discrete manual steps across three tools (Docker CLI, `snow` CLI, SQL), one undocumented workaround that weakens the account's security posture, and roughly 40 minutes of failure diagnosis to identify a network egress problem that surfaced as a generic health check message.

The same class of task - deploy a third-party model, get an HTTP endpoint, call it from SQL - takes three lines of Python for a HuggingFace model, because Snowflake built a first-class path for it.

The gap is not SPCS capability. Every primitive needed is already there: GPU compute pools, block storage with snapshots, secrets, external access integrations, service functions, ingress with PAT auth. The gap is that **nothing composes those primitives on the customer's behalf for a vendor-published inference container.** The customer is the integration layer.

That matters commercially beyond NIMs. The same missing path blocks Kumo (now NVIDIA-owned), and it is the reason a data scientist asking "where do I deploy this model" reaches for Azure AI Foundry instead.

---

## What was actually built

Two NIMs, both on a single `GPU_NV_M` (A10G, 24GB) compute pool in `nims_db.nv`:

| Service | Image | Endpoint | Result |
|---|---|---|---|
| `nim_genmol_svc` | `nvcr.io/nim/nvidia/genmol:2.0.0` | `/generate` | READY. ~4s weight download (~500MB) |
| `nim_llama31_8b_v2` | `nvcr.io/nim/meta/llama-3.1-8b-instruct` | `/v1/chat/completions` | READY. ~2-3 min weight download (~16GB) |

Both are callable over HTTPS with a PAT. GenMol returns QED-scored SMILES that render as 2D structures with RDKit. This works, and it is a legitimate proof point - particularly against the four crash-looped BioNeMo NIMs in the parallel internal effort. GenMol 2.0 is small enough that the weight download completes inside the readiness window, which is precisely why it survived.

Repo layout:

```
sql-setup/1-setup.sql              account objects, compute pool, egress
sql-setup/1a-pull_push.sh          manual docker pull/tag/push (placeholders)
sql-setup/2-genmol-nim-service.sql CREATE SERVICE + status checks
sql-setup/3-llama-nim-service.sql  CREATE SERVICE + status checks
test-notebooks/*.ipynb             PAT auth + requests + RDKit viz
```

---

## The benchmark: what a good third-party model experience looks like today

Snowflake already solved this shape of problem for HuggingFace. This is the bar.

```python
from snowflake.ml.model.models import huggingface

model = huggingface.TransformersPipeline(
    task="text-classification",
    model="ProsusAI/finbert",
)
mv = reg.log_model(model, model_name="finbert", version_name="v1")
mv.create_service(
    service_name="finbert_svc",
    service_compute_pool="my_gpu_pool",
    ingress_enabled=True,
    gpu_requests="1",
)
```

Then from SQL:

```sql
SELECT finbert_svc!__call__('Revenue missed guidance.');
```

What the customer never touches: Docker, image repositories, service specification YAML, resource requests, readiness probes, endpoint discovery, or request payload shape. Signatures are inferred. There is also a Snowsight path - *Import and deploy models from an external service* - which is a Preview feature and, notably, **supports HuggingFace as the only provider**.

That last detail is the whole argument. The provider-catalog-to-endpoint pattern is built, shipped, and in preview. It has exactly one provider wired into it.

What the HuggingFace path gives you that the NIM path does not:

| | HuggingFace via Model Registry | NIM via raw SPCS |
|---|---|---|
| Docker required | No | Yes |
| Image movement | Snowflake builds it | Manual pull / tag / push |
| Egress config | Documented host list | Undocumented, open egress in practice |
| Resource sizing | Defaults, `gpu_requests="1"` | Customer guesses |
| Model versioning | Registry versions | None |
| Lineage | Yes | None |
| Models UI | Yes | Services UI only |
| ML Observability / inference logging | Yes | None |
| SQL invocation | `svc!method()`, auto-generated | Hand-written `CREATE FUNCTION` |
| Autoscaling | `max_instances` | `MIN/MAX_INSTANCES`, manual |
| Failure diagnosis | Build logs, typed errors | Generic probe failure + log spelunking |

A NIM service is, from the platform's point of view, an anonymous container. Everything Snowflake knows about models does not apply to it.

---

## Friction inventory

Ranked by how much each one costs a customer, with the evidence from this deployment.

### 1. Image movement requires a local Docker workstation

To get `nvcr.io/nim/nvidia/genmol:2.0.0` into Snowflake:

```bash
snow spcs image-registry login -c <connection>
docker pull --platform linux/amd64 nvcr.io/nim/nvidia/genmol:2.0
docker tag nvcr.io/nim/nvidia/genmol:2.0 <registry>/<db>/<schema>/<repo>/genmol
docker push <registry>/<db>/<schema>/<repo>/genmol
```

Four commands, one of which moves 34-47GB through a laptop. Problems this creates:

- Regulated-industry customers frequently cannot install Docker on managed endpoints. This is a hard stop, not an inconvenience.
- `docker login` against the Snowflake registry does not play well with MFA.
- The `--platform linux/amd64` flag is mandatory and easy to omit. Omitting it produces an image that fails at runtime on the pool, not at build time.
- The tag has to be reconstructed from `SHOW IMAGE REPOSITORIES` output by hand. My own repo carries the artifact of this: a `1a-pull_push.sh` with `<yourimage-rgistry/genmol>` placeholders, and a service spec where the Llama image is tagged `:linux` while GenMol is `:latest` - because I was hand-managing tags across two attempts.
- Every customer independently mirrors the same public NVIDIA image. There is no shared cache.

**Confirmed as a gap internally.** Novartis ISM response (Tiji Mathew, 2026-06-24): *"Third-party images (e.g., from Docker Hub, NVIDIA NGC) must first be pulled locally and then pushed to a Snowflake-hosted repository - there is no direct pull from external registries at runtime."*

### 2. Egress allowlisting for NGC does not work as documented, and the workaround weakens the account

This one cost the most time and is the most damaging finding.

The intuitive, correctly-scoped network rule:

```sql
CREATE NETWORK RULE ngc_network_rule
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('nvcr.io', 'authn.nvidia.com',
                'helm.ngc.nvidia.com', 'api.ngc.nvidia.com');
```

This **fails**. The NIM SDK authenticates and fetches the file manifest from `api.ngc.nvidia.com` successfully - the logs show `fetching filemap from: https://api.ngc.nvidia.com/v2/org/nim/team/nvidia/models/genmol/2.0.0/files` - and then the actual weight download redirects to CDN hosts that are not in the allowlist. The download hangs, the container exits 1, and the service reports:

```
Readiness probe is failing at path: /v1/health/ready, port: 8000
lastExitCode: 1, restartCount: 1
```

Nothing in that message points at network egress.

The only thing that worked:

```sql
CREATE NETWORK RULE nim_allow_all_egress
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('0.0.0.0:443', '0.0.0.0:80');
```

Unrestricted outbound on 80 and 443 from a GPU container. I would not get that through an InfoSec review at Intuitive, Edwards, or Doximity, and I would not ask. The whole premise we sell for running inference on SPCS is perimeter control. The documented setup path currently requires abandoning it.

For contrast, HuggingFace weight downloads have a **published, enumerated host list** in the docs (`huggingface.co`, `cdn-lfs*.hf.co`, `transfer.xethub.hf.co`, and so on) with an explicit caveat that it can change. NGC has no equivalent.

**No internal findings on this.** No Jira, no PGAP, no Slack thread. This appears to be new evidence.

### 3. Failures surface as a generic health check message

The diagnosis loop for the egress problem:

1. `SYSTEM$GET_SERVICE_STATUS` → "Readiness probe is failing"
2. `SYSTEM$GET_SERVICE_LOGS(..., 200)` → truncated mid-download
3. `SYSTEM$GET_SERVICE_LOGS(..., 500)` → identical output, no further progress
4. `CALL SYSTEM$GET_SERVICE_LOGS(...)` → identical again
5. `DESCRIBE SERVICE` to read back which EAI was actually attached
6. `DESCRIBE NETWORK RULE` on that EAI
7. `SHOW EXTERNAL ACCESS INTEGRATIONS` to find one that might work
8. `DESCRIBE EXTERNAL ACCESS INTEGRATION` on the candidate
9. Compare, infer that CDN redirects are being blocked

Nine steps and an inference to find a blocked egress. The failure modes a NIM can hit are a small, enumerable set - image pull failure, weight download blocked, weight download out of disk, OOM, no GPU profile for the instance family, license/entitlement rejection. Every one of them currently presents as "readiness probe is failing."

The container logs also stop dead at the failure point rather than surfacing the underlying network error, so the most informative signal is absent from the place you would look for it.

### 4. Resource sizing is guesswork, and the wrong answer costs a crash-loop

I set `requests: 16Gi / limits: 24Gi` and one GPU for GenMol. That was a guess. It happened to work because GenMol 2.0 is a small transformer.

There is no published mapping from NIM to required GPU family and memory. The consequences are not theoretical:

- The parallel internal effort crash-looped four NIMs on weight download, exhausting storage or memory.
- Boltz-2 ships **no A10G (`GPU_NV_M`) GPU profile at all**. It silently falls back to a generic profile that may OOM. Nothing catches this at `CREATE SERVICE` time.

A `CREATE SERVICE` that cannot possibly succeed on the chosen instance family should fail immediately with that reason, not after ten minutes of pulling a 40GB image and crash-looping.

### 5. Weights download on every cold start unless you engineer around it

Every NIM downloads weights to `/opt/nim/.cache` on startup. Inspection of the images confirms `/opt/nim/.cache` **does not exist in the image** - the 34-47GB is CUDA and PyTorch runtime. Weights are always a runtime fetch.

For GenMol that is ~4 seconds and irrelevant. For Llama 3.1 8B it is 2-3 minutes. For the larger BioNeMo NIMs it is a 20-50GB download that is the direct cause of the crash-loops.

SPCS already has the right primitive and it is GA: block storage volumes up to 16TB, with snapshots, and `blockConfig.initialContents.fromSnapshot` to seed a new volume from a previous download. Download once, snapshot, seed every subsequent service from the snapshot.

This exact pattern was proposed on **SNOW-1417053** in **May 2024**, in the context of - quoting the ticket - *"the NVIDIA partnership launch of their NIM microservices framework."* Two years later it is still something each customer has to discover and wire up themselves. It is not the default, it is not in any NIM guidance, and I did not configure it in this deployment because nothing prompted me to.

Worth recording the counter-argument to the obvious alternative. Jarek Kowalski (Technical Lead, Container Orchestration) on baking weights into the image: *"A fairly conservative estimate says downloading and unpacking 50GB image will be on the order of 10-20 minutes before a container can even start, and that's only when the image has been properly optimized."* Fat images trade a weight download for a slower image pull. Snapshot-seeded block storage is the better answer.

### 6. NGC credentials have no first-class handling

The NGC API key is a generic string secret:

```sql
CREATE SECRET ngc_api_key TYPE = GENERIC_STRING SECRET_STRING = 'nvapi-...';
```

In practice, during this work, that key also existed in a `docs/scratch.txt` (gitignored, but present), in two `.env` files, and inline in a `docker login` command in shell history. That is the natural consequence of a credential that has to be used in three different places by three different tools with no managed path.

There is also an unresolved licensing question underneath. NIM containers require NVAIE entitlement, and the NGC key is the enforcement point. The recommended fix for cold starts - pre-bake or pre-cache weights and drop the NGC secret - **removes NVIDIA's entitlement checkpoint entirely**. I found no internal guidance on how a customer's NVAIE entitlement is meant to be validated, passed through, or metered when a NIM runs on SPCS. That needs an answer from the partnership, not from engineering.

### 7. The service is invisible to everything Snowflake knows about models

A NIM service is outside Model Registry. No version history, no lineage, no Models UI entry, no ML Observability, no inference logging.

For the HCLS customers I work with this is not a nice-to-have. "Which model version generated this molecule, on what date, and who invoked it" is a GxP-adjacent question. Right now the answer is reconstructed from `SHOW SERVICE CONTAINERS` image digests and query history, by hand.

### 8. Endpoint discovery produces an opaque, unstable URL

`SHOW ENDPOINTS IN SERVICE` returns `boc42qqb-sfsenorthamerica-cguzman-aws-us-west-2.snowflakecomputing.app`. That random prefix ends up hardcoded in notebooks and application config. Both of my notebooks carry it as a string literal. Recreate the service and the consumer breaks.

### 9. SQL-native invocation requires hand-written glue that does not fit the NIM's API

Service functions exist, but the NIM's native payload shape does not match the dataframe protocol Snowflake's own model services use. GenMol's `/generate` takes `{smiles, num_molecules, temperature, noise, gamma, min_add_len, scoring, unique, filter}` and returns `{status, molecules[{smiles, score}]}`.

To call that from SQL, the customer hand-writes a `CREATE FUNCTION ... SERVICE = ... AS '/generate'` and then handles the payload mismatch themselves. Meanwhile every NIM publishes an OpenAPI spec at `/docs`, which is exactly the machine-readable contract needed to generate typed SQL functions automatically. Nothing consumes it.

Both of my notebooks skip service functions entirely and hand-roll `requests.post` with a PAT, because that was faster than fighting the protocol mismatch. That is the tell: the SQL-native path was harder than going around it.

### 10. Cost control is manual discipline

The pool bills per second while active. There is no scale-to-zero for an idle NIM service. Keeping cost sane means remembering to `ALTER COMPUTE POOL ... SUSPEND`, and accepting a 3-5 minute cold start when the next request arrives.

With snapshot-seeded weight caching, warm-start could be fast enough that scale-to-zero becomes the sensible default rather than a trade-off. Those two features are coupled, and together they are the difference between "GPU inference you leave running and pay for" and "GPU inference you pay for when you use."

### 11. There is no way to discover what NIMs exist or what they need

To deploy GenMol I read three pages of NVIDIA documentation to determine the image tag, the port, the health endpoint path, the request schema, and the environment variables. Then I guessed at resources. There is no catalog inside Snowflake that answers: which NIMs are available, which GPU families each supports, what endpoints each exposes, what it costs to run, and whether my account is entitled to it.

---

## What is already in flight

Recommendations should not duplicate these. All PrPr items are internal-only - do not quote timelines to customers.

| Capability | Status | Which friction it addresses | Notes |
|---|---|---|---|
| **SPCS Image Builder** (`snow spcs service build-image`, SnowCLI 3.16) | PrPr | #1 partially | Docker-less, rootless BuildKit in a compute pool. Removes the Docker dependency for *builds*. Does not mirror an existing vendor image. |
| **AWS ECR upstream image repositories** | PrPr, AWS-only | #1 partially | Reference images in customer-owned ECR without duplicating. ACR "next quarter", Docker Hub on roadmap. **`nvcr.io` is not a supported upstream source.** |
| **Block storage volumes + snapshots** | GA (16TB) | #5 | The primitive exists. Nothing applies it to NIMs by default. |
| **GPU Capacity Reservations** (`CREATE COMPUTE RESERVATION`, SNOW-3423213) | Pre-PuPr, AWS-only | GPU availability | Open P0 blockers; pricing not yet defined. |
| **Import and deploy models from external service** | Preview | The whole pattern | **HuggingFace only.** This is the hook an NGC provider would plug into. |
| **PGAP-2791** container image scanning | Ready for PM review | Adjacent | Scanning, not pull-through. |

The gap that matters most and is **not** covered by any of the above: **there is no path from a vendor-published container image to a running, registry-managed SPCS service.**

That has been raised internally as a P0. Akhil Ramasagaram, `#feat-snowpark-container-services_spcs`, 2026-07-20, on Kumo:

> "Today: a customer would docker pull their image, authenticate to Snowflake's image registry, re-push it, write a service spec, and CREATE SERVICE. That's a significant ops barrier for a data scientist who just wants to run tabular ML inference on their Snowflake data. P0 ask: Can we define a first-class deployment path for a partner container - where the customer goes from 'I want to use Kumo' to a running SPCS service without touching Docker?"

And the competitive framing, same thread:

> "Azure AI Foundry has a model catalog where ISVs publish versioned model entries... Customers browse, click 'Use this model,' follow a guided flow, and get a live endpoint - billed through their Azure subscription, no Docker involved. Versioning is platform-managed. We're not in the conversation for 'where do I deploy this model' the way Azure is. That's a distribution problem."

Kumo is now part of NVIDIA. Susan Devitt, `#partners-dcp-all`, 2026-07-29: *"Long term integration plans are still TBD but we will have a path for customer to deploy on snowflake."* Same question, one level up, still open.

---

## Recommendations

### North star: a model provider abstraction

The HuggingFace integration already proves the pattern. Generalize it from one hardcoded provider to a provider concept, and NGC becomes the second instance rather than a bespoke integration.

**Step 1 - register the provider once, at account level.**

```sql
CREATE MODEL PROVIDER nvidia_ngc
  TYPE = NGC
  CREDENTIAL = (API_KEY = 'nvapi-...')
  ENABLED = TRUE;
```

This single object should carry the credential *and* the Snowflake-managed egress allowlist for that provider. The customer never writes a network rule for NGC and never opens `0.0.0.0:443`. Snowflake owns the host list, including CDN hosts, and maintains it as NVIDIA's infrastructure changes. This alone resolves friction #2 and #6 and is the highest-value, lowest-complexity item on this list.

**Step 2 - make the catalog discoverable.**

```sql
SHOW AVAILABLE MODELS IN PROVIDER nvidia_ngc;
```

Returning name, version, task, supported GPU instance families, exposed endpoints, approximate weight size, and entitlement status. Resolves #11, and gives #4 a factual basis.

**Step 3 - deploy without touching Docker or YAML.**

```sql
CREATE MODEL SERVICE genmol
  FROM PROVIDER nvidia_ngc
  MODEL = 'nim/nvidia/genmol:2.0.0'
  IN COMPUTE POOL nim_gpu_pool;
```

Snowflake acquires the image through a pull-through cache, applies the validated resource profile, provisions and seeds a weight-cache volume, and configures the readiness probe from the NIM's known health path. Resolves #1, #4, #5, and the YAML authoring burden.

**Step 4 - land it in Model Registry as a first-class model.** Versioned, lineage-tracked, visible in the Models UI, covered by ML Observability and inference logging. Resolves #7.

**Step 5 - generate the SQL surface from the NIM's OpenAPI spec.**

```sql
SELECT genmol!generate(num_molecules => 10, scoring => 'QED');
```

Every NIM publishes OpenAPI at `/docs`. Introspect it, generate typed service functions, done. Resolves #9 and removes the incentive to bypass SQL entirely.

### Tiered, if the full abstraction is too large to commit to

**P0 - unblocks the security objection, small scope**

1. **`nvcr.io` as a supported upstream image source.** Extend the ECR/ACR upstream work to registries Snowflake curates rather than only customer-owned ones. Highest single-item leverage on friction #1.
2. **A Snowflake-managed egress allowlist for NGC**, shipped either as a built-in network rule or bundled into the provider object. Publish the host list the way the HuggingFace host list is published. Until this exists, the documented path requires open egress and the deployment is not defensible to InfoSec.
3. **Typed failure statuses.** Distinguish image pull failure, weight download blocked, weight download out of space, OOM, no GPU profile for instance family, and entitlement rejection. Surface the underlying network error in container logs rather than stopping at the last successful line.

**P1 - makes it production-shaped**

4. **A published NIM resource profile manifest** (NIM → GPU families, memory, weight size), validated at `CREATE SERVICE` so an impossible configuration fails in seconds with the reason, not in ten minutes with a crash-loop.
5. **Managed weight cache.** One account-level snapshot per NIM version, seeded automatically, shared across services. The primitive is GA; make it the default rather than a discovery.
6. **Scale-to-zero with warm weight cache.** Couples to #5. This is the cost story that makes GPU inference on Snowflake economically obvious rather than a discipline problem.
7. **Stable, nameable service endpoints** so consumers do not hardcode a random prefix.

**P2 - commercial and strategic**

8. **NVAIE entitlement pass-through and metering.** Decide, with the partnership, how entitlement is validated and reported when weights are pre-cached and the NGC key is no longer in the runtime path. There is a commercial opportunity here as well: NIM consumption billed through Snowflake credits is exactly the Azure model, and it is the mechanism that puts Snowflake in the "where do I deploy this" conversation.
9. **NIMs distributed as Native Apps / Marketplace listings.** Marketplace-published apps already get automated container image vulnerability scanning that customer-managed images do not. This is a governance win and a distribution channel in one.
10. **Batch inference over tables as the headline pattern.** The differentiator is not "call an endpoint from Snowflake." It is "score this 10M-row table with a NIM, in place, under RBAC, with lineage." A NIM registered as a backing engine for a custom Cortex AI function - composing with `AI_CLASSIFY`, `AI_FILTER`, `AI_COMPLETE` - is a capability no other platform can match, because no other platform owns the table.

---

## Claims to retire

Carried forward from the parallel internal analysis and already contradicted. Repeating them would undercut the credible gaps.

| Claim | Correction |
|---|---|
| "SPCS has no persistent storage volumes" | Block storage volumes are GA, up to 16TB, with snapshots and snapshot-seeding. Self-corrected in that document on 7/31. |
| "Readiness probe kills the container" | Reviewer response, 7/31: *"Readiness probe will just delay the traffic routing."* |
| "No batch service function calls" | Reviewer response, 7/31: *"We do support batch."* |
| "MSA Search needs 1.4TB; no SPCS volume supports this - Critical" | Likely stale for the same reason as the first row. Re-validate against the 16TB limit before repeating. |

The gaps that survive scrutiny: `nvcr.io` pull-through, the missing partner-container deployment path, the NGC egress allowlist, GPU capacity and instance-family coverage, per-NIM GPU profile validation, opaque failure modes, and NVAIE entitlement handling.

---

## Open questions

1. Is the `go/ai` NIM evaluation restriction rescinded? Who owns the current answer?
2. How is NVAIE entitlement meant to work for a Snowflake customer running a NIM on SPCS - particularly once weights are cached and the NGC key leaves the runtime path?
3. Is the intent to support NIMs as raw SPCS services indefinitely, or to bring them under Model Registry? The answer determines whether the friction list above gets addressed piecemeal or by one abstraction.
4. Does the ECR/ACR upstream repository work have room to include curated third-party registries like `nvcr.io`? Follow-up contact from the Kumo thread: **Pradeep Dorairaj**.
5. Who owns the NIM-to-resource-profile mapping - Snowflake, NVIDIA, or jointly as part of the partnership deliverable?

---

## Contacts

| Area | People |
|---|---|
| SPCS PM | Yavor Georgiev, Seth Mason, Muzz Imam |
| SPCS EM / TPM / Architect | Srikumar Rangarajan, Anurag Bajpai, Derek Denny-Brown |
| NVIDIA partnership / ISV BD | Prerana Gambhir, Nima Badiey, Susan Devitt, Akhil Ramasagaram |
| BioNeMo / HCLS | Kelci Miclaus, Deven Atnoor |
| Upstream registry follow-up | Pradeep Dorairaj |
| Model licensing | Colton Jang |
| Parallel NIM architecture review | Sukirti Gupta |

Channels: `#feat-snowpark-container-services_spcs`, `#spcs-discuss`, `#life-sciences`, `#partners-dcp-all`

---

## Appendix: evidence log

Reproduction of the egress failure, from this deployment.

Status with correctly-scoped NGC network rule:

```
[{"status":"PENDING",
  "message":"Readiness probe is failing at path: /v1/health/ready, port: 8000",
  "containerName":"genmol","instanceId":"0",
  "restartCount":1,"lastExitCode":1,
  "startTime":"2026-06-18T15:20:44Z"}]
```

Container log, final line before the hang:

```
INFO 2026-06-18 15:20:56.889 tokio.rs:916] "nim/nvidia/genmol:2.0.0":
  fetching filemap from: https://api.ngc.nvidia.com/v2/org/nim/team/nvidia/models/genmol/2.0.0/files
```

No further output. No network error surfaced.

Fix applied:

```sql
ALTER SERVICE nims_db.nv.nim_genmol_svc
  SET EXTERNAL_ACCESS_INTEGRATIONS = (NIM_OPEN_EGRESS);
ALTER SERVICE nims_db.nv.nim_genmol_svc SUSPEND;
ALTER SERVICE nims_db.nv.nim_genmol_svc RESUME;
```

Log after, same point in startup:

```
INFO 15:23:08.800 Downloaded filename: len.pk to blob: ...
INFO 15:23:08.834 Downloaded filename: tokenizer.json to blob: ...
INFO 15:23:10.815 Downloaded filename: model_v2.ckpt to blob: ...
INFO 15:23:12.984 http_api.py:201] Readying serving endpoints:
  0.0.0.0:8000/generate (POST)
  0.0.0.0:8000/v1/health/ready (GET)
  0.0.0.0:8000/v1/models (GET)
  ...
```

Status: `READY`. Total weight download: under 4 seconds. The only variable changed was the egress integration.
