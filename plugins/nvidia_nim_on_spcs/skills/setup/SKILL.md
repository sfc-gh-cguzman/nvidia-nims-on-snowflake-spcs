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

### 5a. Prompt for the key

Read it into the environment without echoing it, and without putting it on a command
line (argv is visible to other processes via `ps`):

```bash
read -rsp 'NGC API key (nvapi-...): ' NGC_API_KEY; echo
export NGC_API_KEY
```

### 5b. Preflight it against the registry

```bash
python3 "$PLUGIN_DIR/assets/preflight/check_ngc_key.py" --config "$CONFIG"
```

Three checks, all against the live registry:

| Check | How | Catches |
|---|---|---|
| Key is valid | `GET api.ngc.nvidia.com/v2/users/me` with the key as a bearer token | expired or mistyped key — returns 401 |
| Entitled to each enabled NIM | `proxy_auth` pull scope per repo | valid key with no NVAIE entitlement for that model — returns 403 |
| Configured tag exists | `tags/list` on each repo | a typo or withdrawn version, before the mirror spends four minutes finding out |

Output is one line per check plus an exit code. Passing looks like:

```
[PASS] key            valid, NGC user 'someone'
[PASS] genmol         entitled to nim/nvidia/genmol, tag '2.0' exists
PREFLIGHT PASSED - key is valid and entitled to all 1 enabled NIM(s).
```

**Stop if it fails.** Two failure notes worth relaying to the user:

- An entitlement failure and a wrong repo name are **indistinguishable** — both return
  403. The script says so rather than guessing. Confirm the model at ngc.nvidia.com and
  that the org holds NVAIE entitlement for it.
- A bad-tag failure prints the tags that do exist, which is usually enough to spot the
  problem immediately.

The script never prints the key and never writes it anywhere.

> Scope limit: this validates the key and the entitlement **from wherever you run it**.
> It does not prove SPCS can reach NGC — that depends on the external access
> integration, and a laptop behind a corporate proxy can differ from a compute pool.
> The deploy itself verifies the egress path.

### 5c. Create the secret

Only after the preflight passes. Written through a permission-restricted temp file that
is shredded immediately, so the key never reaches shell history or a command line:

```bash
umask 077
SECRET_SQL="$(mktemp)"
FQ_SECRET=$(python3 -c "import json;c=json.load(open('$CONFIG'));print(f\"{c['target_database']}.{c['target_schema']}.{c['ngc_secret']}\")")
OWNER=$(python3 -c "import json;print(json.load(open('$CONFIG'))['owner_role'])")
cat > "$SECRET_SQL" <<SQL
USE ROLE ${OWNER};
CREATE SECRET IF NOT EXISTS ${FQ_SECRET}
  TYPE = GENERIC_STRING
  SECRET_STRING = '${NGC_API_KEY}'
  COMMENT = 'NGC API key: source-registry auth for image mirroring, and NIM runtime weight download';
SQL
snow sql -f "$SECRET_SQL" -c "$CONN" --enable-templating NONE
shred -u "$SECRET_SQL" 2>/dev/null || rm -f "$SECRET_SQL"
unset NGC_API_KEY
```

Verify without printing it:

```bash
snow sql -c "$CONN" -q "SHOW SECRETS LIKE '%NGC%' IN SCHEMA <db>.<schema>;"
```

> This one secret serves two purposes: the build job uses it to authenticate to the
> source registry, and the running NIM uses it to download weights. If you later
> pre-cache weights and drop it from the service spec, be aware that removes NVIDIA's
> entitlement checkpoint from the runtime path.

## Hand-off

Report what was created, then:

- `/nvidia-nim-on-spcs:mirror-image` — get the NIM images into the account, no Docker
- `/nvidia-nim-on-spcs:deploy-all` — do the rest end to end with cost gates
