---
name: teardown
description: "Remove everything the NIM on SPCS plugin created, or just stop it billing without deleting it. Drops services, the database (including mirrored images), both EAIs, and every compute pool, then verifies nothing was left behind. Triggers: nim teardown, remove nim, uninstall nim, drop nim, clean up nim, stop nim billing, stop gpu spend, delete nim deployment, what is still costing money."
---

# Tear down NIM on SPCS

Two different asks get confused here. Establish which one it is first.

| Ask | Do this |
|---|---|
| "stop it costing money" | suspend the pools. Nothing is deleted, images stay mirrored, restart is a cold start. |
| "remove it" | the teardown script below. |

## Paths & connection (set once)

```bash
PLUGIN_DIR="<absolute path to this plugin>"
WORKDIR="$(mktemp -d)"
CONN="<snowflake connection name>"
CONFIG="$PLUGIN_DIR/config.json"
```

## Option 1: Stop billing, keep the deployment

Cheapest reversible state. Suspending the **pool** is what stops the charge —
suspending only the service can leave nodes up.

```bash
snow sql -c "$CONN" -q "ALTER SERVICE <db>.<schema>.<service> SUSPEND;"
snow sql -c "$CONN" -q "ALTER COMPUTE POOL <gpu_pool> SUSPEND;"
snow sql -c "$CONN" -q "ALTER COMPUTE POOL <build_pool> SUSPEND;"
```

Confirm nothing is left running. Anything not `SUSPENDED` is billing, including
`IDLE`:

```bash
snow sql -c "$CONN" -q "SHOW COMPUTE POOLS;"
```

## Option 2: Full teardown

### Before you run it

Read these out to the user. Both have bitten people.

1. **Mirrored images are inside the database.** `DROP DATABASE` takes the image
   repository with it. Getting them back means a fresh pull from `nvcr.io` — tens
   of GiB and another entitlement check. If the images are the expensive part,
   consider Option 1 instead.
2. **Teardown drops the pools and EAIs NAMED IN `config.json`.** GPU pools are
   expensive and get reused, so two deployments sharing a pool or EAI name is
   common — and tearing down one takes the other's compute. If that is a risk,
   run `DROP DATABASE` on its own and leave the account objects alone:

   ```bash
   snow sql -c "$CONN" -q "DROP DATABASE IF EXISTS <db>;"
   ```

3. **Teardown deletes weight-cache volumes, and leaves snapshots behind.** The
   script's first step is `ALTER COMPUTE POOL ... STOP ALL`, which deletes block
   volumes. With `snapshotOnDelete: true` (the default) each deletion first writes
   a `SYS_BACKUP_ON_DELETE<...>_<timestamp>` snapshot, retained for 7 days. So a
   "complete" teardown still leaves billing snapshots and consumes the
   100-snapshot account limit. Clean them up in Verify below.

### Run it

The rendered script is **inert by default**. It reports what it would do and
changes nothing until you edit it:

```bash
snow sql -f "$WORKDIR/rendered/TEARDOWN.sql" -c "$CONN" \
    --role ACCOUNTADMIN --enable-templating NONE
```

Show the user that dry-run output. Then, on explicit confirmation, flip the flag
and re-run:

```bash
sed -i '' 's/confirmed        BOOLEAN DEFAULT FALSE;/confirmed        BOOLEAN DEFAULT TRUE;/' \
    "$WORKDIR/rendered/TEARDOWN.sql"
snow sql -f "$WORKDIR/rendered/TEARDOWN.sql" -c "$CONN" \
    --role ACCOUNTADMIN --enable-templating NONE
```

The warehouse is kept unless `drop_warehouse` is also set to `TRUE` — it is a
plausible shared name and dropping it can break unrelated work.

Order is deliberate: stop all services on every pool, drop both EAIs (they
reference network rules that live inside the database), drop the database, then
drop the pools. A pool with live services cannot be dropped.

## Verify

The script ends with three counts that should all be `0`. Then confirm nothing
survived under a different name:

```bash
snow sql -c "$CONN" -q "SHOW COMPUTE POOLS;"
snow sql -c "$CONN" -q "SHOW EXTERNAL ACCESS INTEGRATIONS;"
snow sql -c "$CONN" -q "SHOW DATABASES LIKE '<db>';"
```

Snapshots survive `DROP DATABASE` only if they lived in another schema, but the
auto-backup snapshots created during teardown are real and billing. List them
across the account and drop the ones this deployment produced:

```bash
snow sql -c "$CONN" -q "SHOW SNAPSHOTS;"
snow sql -c "$CONN" -q "DROP SNAPSHOT <db>.<schema>.SYS_BACKUP_ON_DELETE...;"
```

If the database is already gone, the snapshots went with it — confirm with
`SHOW SNAPSHOTS` rather than assuming either way.

Then account for the last 24 hours of spend, so the user knows the bleeding
stopped:

```bash
snow sql -c "$CONN" -q "
SELECT compute_pool_name, ROUND(SUM(credits_used), 2) AS credits, MAX(end_time) AS last_seen
FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWPARK_CONTAINER_SERVICES_HISTORY
WHERE start_time > DATEADD(day, -1, CURRENT_TIMESTAMP())
GROUP BY 1 ORDER BY credits DESC;"
```

`ACCOUNT_USAGE` latency is up to a few hours, so a small non-zero number here right
after teardown is expected and is not evidence something is still running. Trust
`SHOW COMPUTE POOLS` for that.

## What teardown does not touch

- The NGC API key itself at ngc.nvidia.com — revoke it there if the evaluation is
  over.
- `config.json` on disk, which names the customer's databases and roles.
- Any PAT created to call the endpoints.
