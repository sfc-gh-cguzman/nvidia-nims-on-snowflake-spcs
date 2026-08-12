---
name: deploy-all
description: "End-to-end NIM on SPCS deployment in one command: account setup, NGC secret, image mirror, GPU services, validation. Confirmation gate before each phase that costs money, and a report of exactly what completed if something fails. Triggers: deploy everything nim, nim end to end, full nim deploy, deploy all nim, stand up nim demo, nim one command, complete nim deployment, nim quickstart."
---

# Deploy NIM on SPCS end to end

Runs every phase in order with a gate before each one that spends. Use this for a
fresh account; use the individual skills to re-run one phase.

## Contract

- **Never skip a gate.** Two phases cost real money: the image mirror (CPU pool +
  tens of GiB egress) and the services (GPU pools with no scale-to-zero).
- **Stop on first failure.** Later phases depend on earlier ones. Do not continue
  past an error hoping it resolves.
- **Report what completed.** If a phase fails, say which phases succeeded, what
  exists in the account, and what it is currently costing. A half-deployed GPU
  pool that nobody knows about is the worst outcome here.

## Paths & connection (set once)

```bash
PLUGIN_DIR="<absolute path to this plugin>"
WORKDIR="$(mktemp -d)"
CONN="<snowflake connection name>"
CONFIG="$PLUGIN_DIR/config.json"
```

## Phase 0: Configure and render — free

Follow `/nvidia-nim-on-spcs:setup` Steps 1 and 2: copy `config.sample.json`,
confirm values with the user, resolve `org`/`account`, render into
`$WORKDIR/rendered`.

Before going further, show the user:

- which NIMs are enabled and what GPU family each will start
- both egress rules and their host lists. Neither uses `0.0.0.0`; the runtime list is
  7 named vendor hosts and is the one a security team will want to see.

## Phase 1: Account and core objects — free

GATE: needs `ACCOUNTADMIN`, and the runtime egress host list is worth an explicit
look. Get agreement on both.

```bash
snow sql -f "$WORKDIR/rendered/00_ACCOUNT_SETUP.sql" -c "$CONN" \
    --role ACCOUNTADMIN --enable-templating NONE
for f in 01_DATABASE_SCHEMA 02_IMAGE_REPOSITORY; do
    snow sql -f "$WORKDIR/rendered/$f.sql" -c "$CONN" --enable-templating NONE
done
```

Pools are created `INITIALLY_SUSPENDED`, so nothing is billing. Confirm it rather
than assuming — a pool that comes up `IDLE` is billing a node with no service on
it, and only `SUSPENDED` is free:

```bash
snow sql -c "$CONN" -q "SHOW COMPUTE POOLS LIKE 'NIM%';"
```

If either pool is not `SUSPENDED`, suspend it now. `AUTO_RESUME = TRUE` brings it
back when Phase 3 and Phase 4 need it, so this costs nothing later.

## Phase 2: NGC key — free

`/nvidia-nim-on-spcs:setup` Step 5, a single command. **Do not run it yourself** — this
is the one step the assistant must not execute.

Tell the user explicitly to **run it in their local terminal** (a normal terminal window
on their own machine, not through the assistant), substituting the real plugin directory
and connection name so it is copy-pasteable:

```
cd <plugin_dir>
python3 assets/preflight/provision_ngc_secret.py --connection <conn>
```

The `!` prefix is **not** a safe substitute. Verified: a secret typed through `!` stays in
the terminal's replay buffer and can be read back by the assistant's own shell afterwards.
Separate terminal window, always.

It prints a 4-character confirmation code the user must type back (the interlock that
proves a live human — `isatty` alone is not sufficient), prompts twice for the key with
echo off, preflights against the live registry, and only then creates the Snowflake
secret. Ask the user to paste back the `[PASS]`/`[OK]` lines, which are safe to share.
**Stop on failure** — an unentitled key otherwise fails in Phase 3 with a bare
`403 Forbidden` after a compute pool has started.

Never route the key through the assistant: chat content is persisted to conversation
history in plaintext, and argv is readable by any local process via `ps`. The script
refuses to run non-interactively for this reason, so it cannot be driven by a tool.

Add `--replace` only when rotating; without it an existing secret is left untouched.

## Phase 3: Mirror the images — COSTS MONEY

GATE. State the size of what is about to be pulled and confirm.

```bash
bash "$WORKDIR/rendered/build_contexts.sh" "$CONN" "$WORKDIR/contexts"
snow sql -f "$WORKDIR/rendered/10_MIRROR_JOBS.sql" -c "$CONN" --enable-templating NONE
```

~82 seconds for the ~10 GiB transfer itself, but budget **~5 minutes for the phase**
— resuming `NIM_BUILD_POOL` from `SUSPENDED` and provisioning the node dominates.
Measured 4m51s end to end on `GEN_X64_G2_32`. Then verify the digest against
upstream (`/nvidia-nim-on-spcs:mirror-image` Step B3) and report the comparison —
a match proves the mirror is the same image, not a rebuild.

Suspend the build pool before moving on:

```bash
snow sql -c "$CONN" -q "ALTER COMPUTE POOL <build_pool> SUSPEND;"
```

## Phase 4: Services — COSTS MONEY, AND KEEPS COSTING

GATE. This is the expensive one. Name each GPU pool and family, and say plainly
that an idle NIM costs the same as a busy one.

```bash
snow sql -f "$WORKDIR/rendered/30_NIM_SERVICES.sql" -c "$CONN" --enable-templating NONE
```

Then poll to READY as in `/nvidia-nim-on-spcs:nim-deploy` Step 2. `PENDING` with
`restartCount = 0` is a normal cold start. Climbing restarts means the container is
exiting — stop and diagnose rather than waiting it out.

## Phase 5: Validate — cheap

```bash
snow sql -f "$WORKDIR/rendered/CHECKS.sql" -c "$CONN" --enable-templating NONE
```

Then make one real inference call per NIM (`/nvidia-nim-on-spcs:nim-deploy`
Step 3). A `READY` service that has never answered a request is not validated.

## Final report

Give the user:

| | |
|---|---|
| Mirrored | image, tag, digest, and whether it matched upstream |
| Running | each service, its pool and family, READY status |
| Endpoints | ingress URL per NIM, and the warning that the prefix changes on recreate |
| Costing right now | which pools are not `SUSPENDED` |
| Open risk | anything CHECKS reported as WARN, including an egress rule still containing `0.0.0.0` |

Then offer:

- `/nvidia-nim-on-spcs:weight-cache` — cache weights so a resume does not re-download
- `/nvidia-nim-on-spcs:nim-ops` — suspend to stop GPU spend, logs, add a NIM
- `/nvidia-nim-on-spcs:teardown` — remove everything

If the user is done evaluating, suspend the GPU pools without being asked to
propose it. Leaving GPU running after a demo is the most common way this gets
expensive.
