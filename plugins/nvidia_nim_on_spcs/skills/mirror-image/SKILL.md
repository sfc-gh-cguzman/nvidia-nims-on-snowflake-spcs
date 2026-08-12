---
name: mirror-image
description: "Get a NIM (or any container image) into a Snowflake image repository with no local Docker, using an in-account SPCS build job. Digest-preserving, streams blobs registry-to-registry, scoped egress. Covers both the shipped `snow spcs service build-image` CLI for public bases and the credential-injected job needed for private registries like nvcr.io. Triggers: mirror nim image, build image in snowflake, no local docker, snow spcs service build-image, nvcr.io into snowflake, image repository, buildkit spcs, pull nim image, mirror container image, docker-less build."
---

# Mirror an image into Snowflake without local Docker

Replaces `docker pull` + `docker tag` + `docker push` from a workstation. The
build runs on a CPU compute pool inside the account, so nothing transits a laptop
and no Docker install is required.

Measured on `nvcr.io/nim/nvidia/genmol:2.0` (9.44 GiB compressed, 98 layers):
**82 seconds wall clock**, and the mirrored image kept the upstream manifest
digest.

## Preconditions

- `/nvidia-nim-on-spcs:setup` has run: build pool, image repository, build stage,
  registry-pull EAI, and the NGC secret all exist.
- `$WORKDIR/rendered` from a render (see setup Step 2). Re-render if config
  changed.

## Paths & connection (set once)

```bash
PLUGIN_DIR="<absolute path to this plugin>"
WORKDIR="$(mktemp -d)"
CONN="<snowflake connection name>"
CONFIG="$PLUGIN_DIR/config.json"
```

## Which path to use

There are two. Pick on one question: **is the base image private?**

| | Base image is public | Base image is private (`nvcr.io`, private ECR, paid Docker Hub) |
|---|---|---|
| Use | `snow spcs service build-image` (Step A) | the rendered mirror job (Step B) |
| Why | it is the shipped, supported command | the CLI has no source-registry credential option |

Every NIM is private, so **Step B is the path for this plugin**. Step A is here
because it is the supported command, it is what should be used the moment it
grows a source-credential flag, and it is the right answer for any public base.

## Step A: the shipped CLI (public base images only)

```bash
SNOWFLAKE_CLI_FEATURES_ENABLE_SPCS_BUILD_IMAGE=true \
snow spcs service build-image \
  -c "$CONN" --database <db> --schema <schema> \
  --compute-pool <build_pool> \
  --image-repository <db>.<schema>.<image_repository> \
  --image-name <name> --image-tag <tag> \
  --build-context-dir <dir containing a Dockerfile> \
  --stage <db>.<schema>.<build_stage> \
  --eai-name <registry_pull_eai>
```

Two things that will bite:

- The command is hidden behind a feature flag and is experimental.
- **Pass `--database` and `--schema` explicitly.** The CLI does not qualify the
  job service name it generates, so even with a fully qualified `--stage` and
  `--image-repository` it fails with `Cannot perform EXECUTE JOB SERVICE. This
  session does not have a current database.`

Against a private base this fails at metadata resolution, and the message names
the real cause:

```
#2 ERROR: failed to authorize: failed to fetch anonymous token:
   GET https://nvcr.io/proxy_auth?scope=repository:nim/nvidia/genmol:pull
   -> 403 Forbidden
```

That is not a misconfiguration. The builder image
(`sf-image-build:0.0.1` — stock `moby/buildkit:v0.18.2-rootless` plus a Go wrapper
at `/usr/local/bin/image-builder`) writes a docker config containing exactly one
credential, the destination Snowflake registry. There is no input for a source
credential. Go to Step B.

## Step B: the credential-injected mirror job

### B1. Upload one build context per NIM

```bash
bash "$WORKDIR/rendered/build_contexts.sh" "$CONN" "$WORKDIR/contexts"
```

Each context is a single-line `FROM <vendor image>` Dockerfile. Because there are
no further instructions, BuildKit exports the resolved layers straight through
without unpacking a filesystem — which is why the mirror is fast and why the
digest survives.

Do not add `--platform`. SPCS build nodes are linux/amd64, so BuildKit picks the
amd64 entry of a multi-arch manifest itself. (In the local-Docker workflow
omitting `--platform linux/amd64` is the classic footgun: you get an arm64 image
that fails at runtime on the pool, not at build time.)

### B2. Run the mirror

MANDATORY STOPPING POINT: this is the first step that costs compute and pulls
tens of GiB. Tell the user which images and how large before running.

```bash
snow sql -f "$WORKDIR/rendered/10_MIRROR_JOBS.sql" -c "$CONN" --enable-templating NONE
```

`EXECUTE JOB SERVICE` is synchronous, so this blocks until each image is
mirrored. Expect ~80 seconds for a ~10 GiB image on `GEN_X64_G2_32`.

The job runs the same builder binary the CLI uses, with `DOCKER_CONFIG` pointed
at a config it writes at container start holding credentials for **both**
registries. The NGC key arrives as a mounted Snowflake secret, so it is never in
the Dockerfile, on the stage, in the SQL, or in shell history.

### B3. Verify

```bash
snow sql -c "$CONN" -q "SHOW IMAGES IN IMAGE REPOSITORY <db>.<schema>.<image_repository>;"
```

Then prove it is the *same image* rather than a rebuild, by comparing the
digest in the Snowflake repository against the upstream manifest. A match means
byte-identical content; `docker pull`/`push` does **not** achieve this because
Docker rewrites the manifest on push.

Ask the user for the NGC key (or read it from their environment), then:

```bash
python3 - <<'PY'
import base64, json, os, urllib.parse, urllib.request
key = os.environ["NGC_KEY"]; repo = "nim/nvidia/genmol"; tag = "2.0"
basic = base64.b64encode(f"$oauthtoken:{key}".encode()).decode()
tok = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://nvcr.io/proxy_auth?" + urllib.parse.urlencode(
        {"account": "$oauthtoken", "scope": f"repository:{repo}:pull"}),
    headers={"Authorization": "Basic " + basic}), timeout=30))["token"]
ml = json.load(urllib.request.urlopen(urllib.request.Request(
    f"https://nvcr.io/v2/{repo}/manifests/{tag}",
    headers={"Authorization": "Bearer " + tok,
             "Accept": "application/vnd.docker.distribution.manifest.list.v2+json"}), timeout=30))
amd = [m for m in ml["manifests"] if m["platform"]["architecture"] == "amd64"][0]
print("upstream amd64 digest:", amd["digest"])
PY
```

Compare that to the `digest` column from `SHOW IMAGES`. Report the comparison
explicitly — it is the strongest evidence the mirror is trustworthy.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `403 Forbidden` on `proxy_auth` | no source credential, **or** the key is not entitled to that NIM | if you are on Step A with a private base, use Step B. If you are already on Step B, run `assets/preflight/check_ngc_key.py --config <config>` — it separates a bad key (401) from a missing entitlement (403). |
| `dial tcp: lookup <host> ... no such host` | the registry redirected blobs to a host not in the pull rule | add that host to `registry_pull_hosts` and re-run setup. Do **not** widen to `0.0.0.0` — an image pull is enumerable. |
| `no space left on device` | image exceeds node storage | every instance family caps at **93 GiB**. Check compressed size first; roughly 3x it for unpacked. |
| Job name already exists | a previous run left the job object | the rendered SQL already does `DROP SERVICE IF EXISTS` first; if you ran a job by hand, drop it |
| `This session does not have a current database` | Step A without `--database`/`--schema` | pass them explicitly |

## Cost hygiene

The build pool bills while active. It has `AUTO_SUSPEND_SECS = 600`, but suspend
it now if no more mirrors are queued:

```bash
snow sql -c "$CONN" -q "ALTER COMPUTE POOL <build_pool> SUSPEND;"
```

## Hand-off

- `/nvidia-nim-on-spcs:nim-deploy` — stand up the GPU services on these images
