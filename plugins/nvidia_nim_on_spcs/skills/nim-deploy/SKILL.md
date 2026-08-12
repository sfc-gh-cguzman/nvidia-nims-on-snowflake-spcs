---
name: nim-deploy
description: "Create the NIM inference services on GPU compute pools from images already mirrored into the account, wait for READY, and validate the endpoint with a real inference call. Triggers: deploy nim service, create nim service, nim endpoint, start nim, nim readiness, validate nim, genmol service, llama nim service, nim inference endpoint, test nim."
---

# Deploy and validate the NIM services

Creates one service per enabled NIM off the mirrored image, waits for the
readiness probe, and proves the endpoint answers.

## Preconditions

- `/nvidia-nim-on-spcs:mirror-image` has completed — the images must already be in
  the account's image repository. A service referencing a missing image sits in
  PENDING with a pull error.
- The runtime EAI exists (created in setup) and the NGC secret exists — a NIM
  needs both to download weights on cold start.
- `owner_role` holds `BIND SERVICE ENDPOINT` on the account for any NIM with
  `public_endpoint: true`. Setup grants this.

## Paths & connection (set once)

```bash
PLUGIN_DIR="<absolute path to this plugin>"
WORKDIR="$(mktemp -d)"
CONN="<snowflake connection name>"
CONFIG="$PLUGIN_DIR/config.json"
```

## Step 1: Create the services

MANDATORY STOPPING POINT: a service starts consuming its GPU pool the moment it
is created, and there is no scale-to-zero for an idle NIM. State which pools and
families are about to start, and confirm.

```bash
snow sql -f "$WORKDIR/rendered/30_NIM_SERVICES.sql" -c "$CONN" --enable-templating NONE
```

`CREATE SERVICE IF NOT EXISTS`, because SPCS does not support `REPLACE SERVICE` —
that is a real error, not an omission. To change a spec on a live service use
`ALTER SERVICE ... FROM SPECIFICATION`, or drop it and re-run this file.

## Step 2: Wait for READY

The file already prints status once. Expect `PENDING` with
`Readiness probe is failing` immediately — weights download on every cold start
because `/opt/nim/.cache` does not exist in the image. The 34-47 GiB in a NIM
image is CUDA and PyTorch runtime, not weights.

```bash
for i in $(seq 1 40); do
  snow sql -c "$CONN" --format json \
    -q "select SYSTEM\$GET_SERVICE_STATUS('<db>.<schema>.<service_name>');" \
  | python3 -c 'import json,sys
for c in json.loads(list(json.load(sys.stdin)[0].values())[0]):
    print(c.get("status"), c.get("message"), "restarts=", c.get("restartCount"))'
  sleep 15
done
```

Read it this way:

| Observation | Meaning |
|---|---|
| `PENDING`, `restartCount = 0` | normal. Weights are downloading. genmol ~5s, llama-3.1-8b 2-3 min, larger NIMs 20-50 GiB. |
| `PENDING`, `restartCount` climbing, `lastExitCode = 1` | the container is exiting. **Almost always blocked egress on the weight download.** |
| `READY` | done. |

### The failure that looks like success

A fatal engine-initialisation error can surface as:

```
status: DONE      restartCount: 5      lastExitCode: 0
message: Completed successfully
```

Observed on llama-3.1-8b deployed without `NIM_MAX_MODEL_LEN`: vLLM could not
allocate KV cache, `EngineCore failed to start`, the container exited, and after five
restarts the service reported DONE with exit code 0. **A non-zero `restartCount` on a
service reporting DONE means it failed.** Never treat DONE as healthy without either
a `restartCount` of 0 or a successful inference call.

The root cause is only in the container log. For a GPU memory failure look for the
`NIM GPU Memory Report` block and the line
`estimated maximum model length is <N>` — set `NIM_MAX_MODEL_LEN` below that value in
the NIM's `env` in `config.json`.

### If restarts are climbing

`Readiness probe is failing` is the single message the platform emits for a set of
distinct failures: image pull failure, weight download blocked, weight download
out of disk, OOM, no GPU profile for the instance family, and entitlement
rejection. Distinguish them from the container log, not the status:

```bash
snow sql -c "$CONN" -q "select SYSTEM\$GET_SERVICE_LOGS('<db>.<schema>.<service>', '0', '<container>', 200);"
```

The tell for blocked egress is the log **stopping dead** right after
`fetching filemap from: https://api.ngc.nvidia.com/...` with no network error
reported. The underlying error is not surfaced. If you see that, the weight
download is being blocked — widen `runtime_egress_hosts` (see setup Step 3) and
then:

```bash
snow sql -c "$CONN" -q "
ALTER SERVICE <db>.<schema>.<service> SET EXTERNAL_ACCESS_INTEGRATIONS = (<runtime_eai>);
ALTER SERVICE <db>.<schema>.<service> SUSPEND;
ALTER SERVICE <db>.<schema>.<service> RESUME;"
```

Other signatures: an OOM or a missing GPU profile shows up as the container dying
*after* weights land; out-of-disk shows as the download itself failing partway.

## Step 3: Validate with a real call

Three ways, in order of preference. **Prefer the in-account job** — it needs no
credential at all. If you specifically need to prove the *external ingress* path works,
use the session token in 3b rather than minting a PAT.

### 3a. From inside the account, no PAT (recommended)

Service-to-service traffic uses the internal DNS name and requires no authentication
header. Get the name from `SHOW SERVICES` (`dns_name` column, e.g.
`nim-genmol-svc.ngep.svc.spcs.internal`), then call it from a one-shot job on any CPU
pool, reusing the NIM's own image because it already has Python:

```sql
EXECUTE JOB SERVICE
  IN COMPUTE POOL <build_pool>
  NAME = <db>.<schema>.GENMOL_INFERENCE_TEST
  FROM SPECIFICATION $$
spec:
  containers:
    - name: caller
      image: <mirrored image>
      command: ["/bin/sh"]
      args:
        - -c
        - |
          python3 - <<'PY'
          import json, urllib.request
          url = "http://<dns_name>:8000/generate"
          body = json.dumps({"num_molecules": 5, "scoring": "QED", "unique": True}).encode()
          r = urllib.request.urlopen(urllib.request.Request(
                  url, data=body, headers={"Content-Type": "application/json"}), timeout=120)
          d = json.load(r); print("HTTP", r.status, d.get("status"))
          for m in d.get("molecules", []): print(f"  QED {m['score']:.3f}  {m['smiles']}")
          PY
      resources:
        requests: { cpu: 1, memory: 2Gi }
  $$;
```

Then read the result with `SYSTEM$GET_SERVICE_LOGS(..., 'caller', 60)`. A `HTTP 200` with
scored molecules is the proof the mirrored image is functional, not merely present.

No external access integration is needed — the traffic never leaves the account.

### 3b. From outside, no PAT (session token)

The Python Connector can issue a **session token** off a connection that is already
authenticated in `connections.toml` — whatever the mechanism (local OAuth, SSO, key
pair). The SPCS ingress accepts it in exactly the same header a PAT uses, so this
validates the real external path without creating a new credential.

```bash
python plugins/nvidia_nim_on_spcs/assets/validate/spcs_call.py \
  --connection "$CONN" --service <db>.<schema>.<service> \
  --path /generate --json '{"num_molecules": 5, "scoring": "QED"}'
```

Run it with `--token-only` first. That acquires the token and reports its length
without calling anything, so you can confirm auth works *before* resuming a GPU pool.

Two constraints worth knowing:

- The connector must be importable by the interpreter you invoke. A system `python3`
  frequently lacks it while a conda or venv python has it — check with
  `python -c "import snowflake.connector"` rather than assuming.
- It calls `conn._rest._token_request('ISSUE')`, a **private** connector API that
  Snowflake documents with no forward-compatibility guarantee. Fine for validation,
  not for anything long-lived. Use a PAT for that.

### 3c. From outside, with a PAT

`SHOW ENDPOINTS` returns the ingress URL. Note the random prefix — it changes if the
service is recreated, so do not hardcode it:

```bash
snow sql -c "$CONN" -q "SHOW ENDPOINTS IN SERVICE <db>.<schema>.<service>;"

curl -sS -X POST "https://<ingress_url>/generate" \
  -H "Content-Type: application/json" \
  -H "Authorization: Snowflake Token=\"$SNOWFLAKE_PAT\"" \
  -d '{"num_molecules": 5, "scoring": "QED", "unique": true}'
```

For an LLM NIM the path is `/v1/chat/completions` with the usual OpenAI-shaped body.

PAT gotchas, all of which present as the same unhelpful
`{"detail":"Authorization token is invalid"}`:

- PATs are **account-scoped**. A token for one account will not work against another.
- List them with `SHOW USER PROGRAMMATIC ACCESS TOKENS`. The form
  `SHOW PROGRAMMATIC ACCESS TOKENS FOR USER <name>` does not parse.
- An empty or truncated token produces the identical error, so verify the variable is
  non-empty before blaming the service.

## Step 4: Run the checks

```bash
snow sql -f "$WORKDIR/rendered/CHECKS.sql" -c "$CONN" --enable-templating NONE
```

Every row is `CHECK_NAME | STATUS | DETAIL`. `PASS` and `WARN` are both
acceptable. `runtime_egress_scoped` returning `WARN` is expected with the default
config and is reported deliberately — it is the finding to raise with the
customer's security team, not something to hide.

## What this deployment does not give you

Worth stating plainly, because customers assume otherwise. A NIM service is an
anonymous container to the platform: no Model Registry entry, no version history,
no lineage, no Models UI, no ML Observability, no inference logging. "Which model
version produced this output, when, invoked by whom" has to be reconstructed by
hand from `SHOW SERVICE CONTAINERS` image digests plus query history. For a
GxP-adjacent audit trail, record the mirrored digest at deploy time — the
mirror-image skill already surfaces it.

## Hand-off

- `/nvidia-nim-on-spcs:weight-cache` — stop re-downloading weights on every cold start
- `/nvidia-nim-on-spcs:nim-ops` — status, logs, suspend/resume, cost control
- `/nvidia-nim-on-spcs:teardown` — stop billing entirely
