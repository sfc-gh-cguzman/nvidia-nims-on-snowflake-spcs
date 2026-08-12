---
name: nim-ops
description: "Operate running NIM services: status and readiness, container logs, endpoint discovery, suspend and resume to control GPU spend, add a NIM to an existing deployment, and diagnose a service that will not become ready. Triggers: nim status, nim logs, nim not ready, readiness probe failing, suspend nim, resume nim, nim cost, nim gpu spend, add a nim, nim endpoint url, nim ops, nim monitoring, nim troubleshooting."
---

# Operate the NIM services

Day-two operations. Nothing here creates account objects.

## Paths & connection (set once)

```bash
PLUGIN_DIR="<absolute path to this plugin>"
CONN="<snowflake connection name>"
CONFIG="$PLUGIN_DIR/config.json"
```

Resolve names from config rather than asking:

```bash
python3 -c "import json;c=json.load(open('$CONFIG'));print('\n'.join(
  f\"{n['key']}: {c['target_database']}.{c['target_schema']}.{n['service_name']} on {n['gpu_pool']}\"
  for n in c['nims'] if n.get('enabled')))"
```

## Inventory

```bash
snow sql -c "$CONN" -q "SHOW SERVICES IN SCHEMA <db>.<schema>;"
snow sql -c "$CONN" -q "SHOW COMPUTE POOLS;"
snow sql -c "$CONN" -q "SHOW IMAGES IN IMAGE REPOSITORY <db>.<schema>.<image_repository>;"
```

Pool states: `IDLE` means nodes are up with nothing running and **is still
billing**. `SUSPENDED` is the only state that is not.

## Status and logs

```bash
snow sql -c "$CONN" -q "select SYSTEM\$GET_SERVICE_STATUS('<db>.<schema>.<service>');"
snow sql -c "$CONN" -q "select SYSTEM\$GET_SERVICE_LOGS('<db>.<schema>.<service>', '0', '<container>', 200);"
```

The container name is the NIM `key` from config, not the service name.

`SHOW SERVICE CONTAINERS IN SERVICE <service>` gives the running image digest,
which is what to record for an audit trail — the platform keeps no version
history for a NIM.

## Reading PENDING messages

`PENDING` is not one state. The `message` field distinguishes them, and only the
last one is a fault:

| Message | Meaning |
|---|---|
| `Compute pool node(s) are being provisioned` | pool is starting. Wait. |
| `Unschedulable due to insufficient GPU resources.` | no GPU free for this request. Usually another pool in the account still holds the capacity — a pool in `STOPPING` has not released its nodes yet. Also appears when the region is out of that family. Check `SHOW COMPUTE POOLS` for pools that are not `SUSPENDED`. |
| `Waiting to start` | scheduled, image being pulled. |
| `Readiness probe is failing at path: ...` | container is up, weights downloading. Only a fault if `restartCount` climbs. |

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

## Diagnosing "Readiness probe is failing"

This one message covers image pull failure, blocked weight download, out of disk,
OOM, no GPU profile for the instance family, and entitlement rejection. Work
through it in this order rather than guessing:

1. **`restartCount`** — `0` with `PENDING` is a normal cold start, not a fault.
   Weights download on every start.
2. **Container log tail.** The signature of blocked egress is the log stopping
   dead immediately after
   `fetching filemap from: https://api.ngc.nvidia.com/...` with no error line.
   The underlying network error is never surfaced, so absence of an error *is*
   the signal.
3. **Which EAI is actually attached** — `DESCRIBE SERVICE` then
   `DESCRIBE EXTERNAL ACCESS INTEGRATION` and `DESCRIBE NETWORK RULE`. It is easy
   to attach the scoped pull EAI to a service that needs the runtime one.
4. **Image present?** `SHOW IMAGES IN IMAGE REPOSITORY`. A missing or mistyped tag
   presents the same way.
5. **GPU profile.** If weights land and the container then dies, suspect the
   instance family. Some NIMs ship no profile for smaller families and fall back
   to a generic one that OOMs. Nothing validates this at `CREATE SERVICE` time.

Fix and restart:

```bash
snow sql -c "$CONN" -q "
ALTER SERVICE <db>.<schema>.<service> SET EXTERNAL_ACCESS_INTEGRATIONS = (<runtime_eai>);
ALTER SERVICE <db>.<schema>.<service> SUSPEND;
ALTER SERVICE <db>.<schema>.<service> RESUME;"
```

## Endpoints

```bash
snow sql -c "$CONN" -q "SHOW ENDPOINTS IN SERVICE <db>.<schema>.<service>;"
```

The `ingress_url` carries a random prefix that is regenerated when the service is
recreated. Anything holding it — notebooks, app config, an API gateway — breaks on
recreate. Resolve it at call time from `SHOW ENDPOINTS` instead of hardcoding it.

## Controlling GPU spend

This is the main operational lever. A NIM has no scale-to-zero: an idle service
costs the same as a busy one.

```bash
# stop paying, keep the service definition and the mirrored image
snow sql -c "$CONN" -q "ALTER SERVICE <db>.<schema>.<service> SUSPEND;"
snow sql -c "$CONN" -q "ALTER COMPUTE POOL <gpu_pool> SUSPEND;"

# back up: expect a 3-5 minute cold start, weights download again
snow sql -c "$CONN" -q "ALTER COMPUTE POOL <gpu_pool> RESUME;"
snow sql -c "$CONN" -q "ALTER SERVICE <db>.<schema>.<service> RESUME;"
```

Suspending the pool is what actually stops the charge — suspending only the
service can leave nodes running.

### If the NIM has a weight cache

Two things change, and one of them is a trap.

- **Suspend/resume preserves the block volume**, and Snowflake reattaches it to
  the same instance ID. The weights are still there, so a resume does not
  re-download them. You keep paying for the *volume* while suspended, which is a
  small fraction of a GPU node. This is what makes suspend-when-idle the default
  rather than a trade-off.
- **`ALTER COMPUTE POOL ... STOP ALL` DELETES the block volume.** So does
  `DROP SERVICE ... FORCE` and `ALTER SERVICE ... RESTORE VOLUME`. Use
  `ALTER COMPUTE POOL ... SUSPEND`, never `STOP ALL`, on a pool whose services
  hold a cache you want to keep.

With `snapshotOnDelete: true` (the plugin's default) a deletion takes an automatic
`SYS_BACKUP_ON_DELETE<...>_<timestamp>` snapshot first, retained for
`snapshotDeleteAfter` (7 days by default). That protects you from losing the
cache, but the snapshots bill and count against the **100-snapshot account
limit**, so prune them:

```bash
snow sql -c "$CONN" -q "SHOW SNAPSHOTS IN SCHEMA <db>.<schema>;"
snow sql -c "$CONN" -q "DROP SNAPSHOT <db>.<schema>.SYS_BACKUP_ON_DELETE...;"
```

A dropped snapshot still bills through its data retention period (1 day default).

Credits consumed per pool:

```bash
snow sql -c "$CONN" -q "
SELECT compute_pool_name, ROUND(SUM(credits_used), 2) AS credits,
       MIN(start_time) AS since
FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWPARK_CONTAINER_SERVICES_HISTORY
WHERE start_time > DATEADD(day, -7, CURRENT_TIMESTAMP())
GROUP BY 1 ORDER BY credits DESC;"
```

> The cold-start cost and the lack of scale-to-zero are coupled. SPCS block
> storage volumes are GA up to 16 TB with snapshots, and
> `blockConfig.initialContents.fromSnapshot` can seed a new volume from a previous
> download — download weights once, snapshot, seed every later service. That turns
> a 20-50 GiB cold start into a warm mount and makes suspend-when-idle the obvious
> default rather than a trade-off. This plugin does not configure it; it is the
> highest-value thing to add next.

## Adding a NIM to an existing deployment

Every object is `CREATE ... IF NOT EXISTS` and every template loops over enabled
NIMs, so this is additive and safe:

1. Add the entry to `nims` in `config.json` with `"enabled": true`.
2. Re-render (setup Step 2).
3. Re-run `00_ACCOUNT_SETUP.sql` as ACCOUNTADMIN — creates only the new GPU pool.
4. `/nvidia-nim-on-spcs:mirror-image` — mirrors only what is missing; existing
   images are untouched.
5. `/nvidia-nim-on-spcs:nim-deploy` — `IF NOT EXISTS` skips services that exist.

Re-rendering with a NIM flipped to `"enabled": false` does **not** remove
anything already deployed. Drop it explicitly:

```bash
snow sql -c "$CONN" -q "DROP SERVICE IF EXISTS <db>.<schema>.<service>;"
snow sql -c "$CONN" -q "DROP COMPUTE POOL IF EXISTS <gpu_pool>;"
```

## Re-running the invariant checks

```bash
snow sql -f "$WORKDIR/rendered/CHECKS.sql" -c "$CONN" --enable-templating NONE
```
