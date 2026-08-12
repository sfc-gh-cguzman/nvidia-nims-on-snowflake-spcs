---
name: setup
description: "Provision the account objects a NIM deployment needs: build pool, one GPU pool per NIM, scoped registry-pull egress, runtime egress, image repository, build stage, and the NGC API key secret. Run once per account before mirroring images. Triggers: nim setup, setup nim on spcs, provision nim account objects, nim compute pools, ngc secret, nim egress, nim prerequisites, prepare account for nim."
---

# Set up an account for NIM on SPCS

Creates everything the build and serve phases depend on. Idempotent — every
object is `CREATE ... IF NOT EXISTS`, so re-running is safe and picks up new NIMs
added to `config.json`.

## Preconditions

- `snow` CLI with a named connection to the target account.
- **Two roles.** The `account` phase needs `ACCOUNTADMIN` (compute pools, network
  rules, and external access integrations cannot be created by `SYSADMIN`).
  Everything after it runs as `owner_role` from config. If you do not hold
  ACCOUNTADMIN, hand that one rendered file to whoever does.
- `uv` available locally (the renderer needs `jinja2` + `pyyaml`).
- An **NGC API key** (`nvapi-...`) from ngc.nvidia.com with entitlement to the
  NIMs you intend to run. NIM containers require NVAIE entitlement; the NGC key
  is the enforcement point.
- GPU capacity in the target region for each `gpu_pool_family` in config.

## Paths & connection (set once)

```bash
PLUGIN_DIR="<absolute path to this plugin>"
WORKDIR="$(mktemp -d)"
CONN="<snowflake connection name>"
CONFIG="$PLUGIN_DIR/config.json"
```

Never write into `$PLUGIN_DIR` — all rendered output goes to `$WORKDIR`.

## Step 1: Configuration

If `$CONFIG` does not exist, copy the template and confirm values with the user
before rendering:

```bash
cp "$PLUGIN_DIR/assets/config.sample.json" "$CONFIG"
```

Ask about these; the rest have sane defaults:

| Key | Ask because |
|---|---|
| `target_database` / `target_schema` | where the image repository, stage, secret, and services land |
| `owner_role` | owns everything from the `core` phase onward |
| `nims[].enabled` | each enabled NIM gets its own GPU pool and a full image mirror. `genmol` is on by default because it is small and fast; `llama-3.1-8b-instruct` is off. |
| `nims[].gpu_pool_family` | must exist in the region and be large enough for the model. There is no published NIM-to-GPU-family mapping, so this is a judgement call — see the note below. |
| `nims[].env` | model-specific environment variables passed straight to the container. Some NIMs will not start without one — llama-3.1-8b on a single A10G needs `NIM_MAX_MODEL_LEN` below 34432 or vLLM cannot allocate KV cache. |
| `runtime_egress_hosts` | **security decision.** Defaults to 7 named vendor hosts, no `0.0.0.0`. Read Step 3 — the list is a union across the shipped NIMs and may need a host added for a NIM you bring. |

`config.json` is read as strict JSON — **no comments**. It is gitignored; do not
commit it.

> On GPU sizing: a `CREATE SERVICE` that cannot succeed on the chosen family does
> not fail fast. It pulls the image, starts, and crash-loops on weight download
> ten minutes later. Some NIMs ship no profile for smaller families at all
> (Boltz-2 has no `GPU_NV_M` profile) and silently fall back to a generic profile
> that may OOM. Check the vendor's model card for supported GPUs before enabling
> a NIM that is not in the shipped catalog.

## Step 2: Resolve the account and render

The registry hostnames are properties of the account, not of the deployment, so
they are passed to the renderer rather than stored in config:

```bash
eval "$(snow sql -c "$CONN" --format json \
  -q "select current_organization_name() as o, current_account_name() as a;" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)[0]; print(f"ORG={d[\"O\"]}\nACCT={d[\"A\"]}")')"

uv run --with jinja2 --with pyyaml python "$PLUGIN_DIR/assets/renderer/render.py" \
  --manifest "$PLUGIN_DIR/assets/build_manifest.yaml" \
  --templates "$PLUGIN_DIR/assets/templates" \
  --out "$WORKDIR/rendered" \
  --config "$CONFIG" \
  --set "org=$ORG" "account=$ACCT"
```

The renderer uses `StrictUndefined`, so a template referencing a key that
`config.json` omits fails here rather than emitting a broken `CREATE`. It also
rejects a config where no NIM is enabled, or where two NIMs share a `key` (which
would silently overwrite one mirrored image with another).

## Step 3: Account phase (ACCOUNTADMIN)

MANDATORY STOPPING POINT. Show the user both egress rules and get agreement. Neither
uses `0.0.0.0` — both are scoped to named vendor hosts — but the runtime one is the
list a security team will want to review.

**Image pull** — 4 hosts: `nvcr.io`, `layers.nvcr.io`, `authn.nvidia.com`,
`api.ngc.nvidia.com`.

**Runtime weight download** — 7 hosts, derived empirically and verified:

| Host | Why |
|---|---|
| `api.ngc.nvidia.com:443` | the file manifest |
| `xfiles.ngc.nvidia.com:443` | every weight blob, HTTP 200, served directly with no onward redirect |
| `authn.nvidia.com:443` | token exchange |
| `huggingface.co:443` | genmol's tokenizer (`datamol-io/safe-gpt`), fetched at model-init |
| `cdn-lfs.hf.co:443` | HF LFS redirect targets, precautionary |
| `cdn-lfs-us-1.hf.co:443` | |
| `transfer.xethub.hf.co:443` | |

Two things to tell the user honestly:

1. **This list is a union across the shipped catalog and may be incomplete for a NIM
   you add.** genmol needs the HuggingFace hosts; llama-3.1-8b does not. There is no
   published per-NIM dependency manifest.
2. **The discovery loop is reliable, because a blocked host names itself.** Deploy,
   read the container log, add whatever host it names, repeat:

   ```
   Failed to resolve 'huggingface.co' ([Errno -2] Name or service not known)
   dial tcp: lookup <host> on 169.254.20.10:53: no such host
   ```

   Do not widen to `0.0.0.0`. Nothing observed so far requires it. And be aware a
   blocked host can surface as something that looks unrelated — genmol turned a DNS
   failure into `TypeError: stat: path should be string, bytes, os.PathLike or
   integer, not NoneType` several frames into a tokenizer library.

```bash
snow sql -f "$WORKDIR/rendered/00_ACCOUNT_SETUP.sql" -c "$CONN" \
    --role ACCOUNTADMIN --enable-templating NONE
```

Always pass `--enable-templating NONE`. The CLI treats `&` as a variable marker
and these files contain `&` in comments.

## Step 4: Core phase

```bash
for f in 01_DATABASE_SCHEMA 02_IMAGE_REPOSITORY; do
    snow sql -f "$WORKDIR/rendered/$f.sql" -c "$CONN" --enable-templating NONE
done
```

## Step 5: NGC key — validate first, then create the secret

Do not create the secret from an unvalidated key. A bad or unentitled key does not fail
here, it fails much later in the build phase with a bare `403 Forbidden` after a compute
pool has already started.

### 5a. One command: prompt, validate, create

**Do not run this yourself.** This is the one step in the whole plugin the assistant
must not execute. Stop, hand the command to the user, and wait for them to report back.

Say this to them, explicitly — do not just print the command and assume they know where
to run it:

> **Run this in your local terminal** (a normal terminal window on your own machine,
> not through me). It will prompt you for your NGC API key twice, with the input
> hidden. I never see the key — it goes straight from your keyboard into a Snowflake
> secret.
>
> ```
> cd <plugin_dir>
> python3 assets/preflight/provision_ngc_secret.py --connection <conn>
> ```
>
> Paste back the `[PASS]` / `[OK]` lines when it finishes. Those are safe to share —
> the script never prints the key.

Substitute the real plugin directory and connection name before showing it, so the user
can copy-paste without editing anything.

The `!` prefix inside this session is **not** a safe substitute, even though it does give
a real TTY. Verified on 2026-08-12: after a key was typed through `!`, two consecutive
`getpass` reads from the assistant's own shell each returned that same 70-character key
from the terminal's replay buffer. A secret typed into `!` is readable by the assistant's
shell afterwards, which defeats the entire point. **Always a separate terminal window.**

Add `--config <path>` if config.json is not at the plugin root, and `--replace` to
rotate an existing secret (without it, an existing secret is left untouched and the
script exits 1, so a re-run is never destructive).

It does three things in order and stops at the first failure:

1. **Prints a 4-character confirmation code** and requires the user to type it back.
   This is the interlock that proves a live human, not double-entry — see below.
2. **Prompts twice** for the key with echo off, and requires the two entries to match.
3. **Preflights** it against the live registry - key validity, per-NIM entitlement, and
   tag existence (the table in 5b below).
4. **Creates the secret** only if every check passed.

If the user reports the confirmation code never appeared, or that it "did not match" when
they never got to type it, they ran it somewhere whose input is replayed from a buffer —
an assistant tool, a CI step, a piped shell. Point them at a real terminal window rather
than working around the check. The code prompt times out after 90 seconds and fails
closed rather than hanging.

Expected output, all of which is safe to show anyone:

```
[PASS] key            valid, NGC user 'someone'
[PASS] genmol         entitled to nim/nvidia/genmol, tag '2.0' exists
[OK]   created NIMS_DB.NV.NGC_API_KEY
       name=NGC_API_KEY  type=GENERIC_STRING  created=...
```

#### Why it is built this way

**Never route a secret through the assistant.** Not via a question, not pasted into
chat, not on a command line. Anything in a conversation is written to
`~/.snowflake/cortex/conversations/<id>.json` in plaintext and kept; argv is readable by
any local process via `ps`. The assistant orchestrates around this command and never
sees the value.

**Why a confirmation code, and why `isatty` is not enough.** Inside an assistant's
pseudo-terminal `sys.stdin.isatty()` returns **True** and reads do **not** block — the pty
replays whatever is already in its buffer, returning the *same* value on every read.
Measured: two consecutive `getpass` calls in an assistant shell each returned an identical
70-character key with no human present. So neither `isatty` nor double-entry can
distinguish a person from a replayed buffer; double-entry passes trivially, because a
replay matches itself. A nonce generated *after* the process starts cannot be in a buffer
filled before it started, so echoing it back is real proof of a live human. Verified both
directions: wrong code refuses and creates nothing; correct code opens the gate.

**Why the key must not be typed into `!`.** Same mechanism, and this is the practical
consequence: a secret entered through `!` remains in the terminal's replay buffer and can
be read back by the assistant's shell afterwards. Use a separate terminal window.

**Why not a shell `read`.** The hidden-read flag is not portable. `read -rsp 'p' VAR` is
bash; under zsh `-p` means *read from coprocess*, so that line fails with
`read: -p: no coprocess` and leaves the variable **empty** - which creates an empty
secret that surfaces much later as an opaque auth error. The zsh form is
`read -rs "?prompt"` into `$REPLY`. Python's getpass behaves the same on both, so the
script uses it and the question disappears.

**Why a SQL literal is acceptable here.** `CREATE SECRET` takes no bind parameters, so
the value must be inlined. Verified safe: Snowflake redacts `SECRET_STRING` in query
history - a probe secret created with a known canary string stored as
`SECRET_STRING = '☺☺☺☺☺'`, with the canary absent from
`QUERY_HISTORY`. The script also skips the temp-file-and-shred dance entirely by going
through the Python connector, so the key never touches disk.

**Requirement.** `snowflake-connector-python` must be importable by the interpreter you
invoke. A system `python3` frequently lacks it while a conda or venv python has it;
check with `python -c "import snowflake.connector"` rather than assuming.

### 5b. What the preflight checks (reference)

All three run against the live registry, inside the command above. `check_ngc_key.py`
can also be run standalone against an exported `NGC_API_KEY` if you only want to
validate without creating anything.

| Check | How | Catches |
|---|---|---|
| Key is valid | `GET api.ngc.nvidia.com/v2/users/me` with the key as a bearer token | expired or mistyped key — returns 401 |
| Entitled to each enabled NIM | `proxy_auth` pull scope per repo | valid key with no NVAIE entitlement for that model — returns 403 |
| Configured tag exists | `tags/list` on each repo | a typo or withdrawn version, before the mirror spends four minutes finding out |

Two failure notes worth relaying to the user:

- An entitlement failure and a wrong repo name are **indistinguishable** — both return
  403. The script says so rather than guessing. Confirm the model at ngc.nvidia.com and
  that the org holds NVAIE entitlement for it.
- A bad-tag failure prints the tags that do exist, which is usually enough to spot the
  problem immediately.

> Scope limit: this validates the key and the entitlement **from wherever you run it**.
> It does not prove SPCS can reach NGC — that depends on the external access
> integration, and a laptop behind a corporate proxy can differ from a compute pool.
> The deploy itself verifies the egress path.

### 5c. Confirm the secret exists

The script prints this itself, but to re-check later without printing the value:

```bash
snow sql -c "$CONN" -q "SHOW SECRETS LIKE '%NGC%' IN SCHEMA <db>.<schema>;"
```

There is no way to read a secret's value back out via SQL, by design — so if the key is
ever lost, rotate it with `--replace` rather than trying to recover it.

> This one secret serves two purposes: the build job uses it to authenticate to the
> source registry, and the running NIM uses it to download weights. If you later
> pre-cache weights and drop it from the service spec, be aware that removes NVIDIA's
> entitlement checkpoint from the runtime path.

## Hand-off

Report what was created, then:

- `/nvidia-nim-on-spcs:mirror-image` — get the NIM images into the account, no Docker
- `/nvidia-nim-on-spcs:deploy-all` — do the rest end to end with cost gates
