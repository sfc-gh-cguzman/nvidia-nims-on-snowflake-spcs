/* #########################################################
MIRROR AN NVIDIA NIM IMAGE INTO SNOWFLAKE WITH NO LOCAL DOCKER

Replaces sql-setup/1a-pull_push.sh (docker pull / tag / push from a laptop).

Why this is a hand-rolled EXECUTE JOB SERVICE and not `snow spcs service build-image`:
  `snow spcs service build-image` (SnowCLI 3.20, PrPr, behind
  SNOWFLAKE_CLI_FEATURES_ENABLE_SPCS_BUILD_IMAGE) runs a fixed job service on the
  image `/snowflake/images/snowflake_images/sf-image-build:0.0.1`. That image is
  stock moby/buildkit v0.18.2-rootless plus a Go wrapper at
  /usr/local/bin/image-builder. The wrapper's setupRegistryCredentials writes
  $HOME/.docker/config.json containing exactly one entry: the Snowflake registry,
  authenticated as 0auth2accesstoken with the token from /snowflake/session/token.
  There is no CLI option and no env var for SOURCE registry credentials, so a
  Dockerfile whose base image is private fails at metadata resolution:

    #2 ERROR: failed to authorize: failed to fetch anonymous token:
       GET https://nvcr.io/proxy_auth?scope=repository:nim/nvidia/genmol:pull
       -> 403 Forbidden

  BuildKit resolves registry auth through $DOCKER_CONFIG/config.json, and the job
  spec does let us set env vars and mount secrets. So this job runs the same
  builder binary with a DOCKER_CONFIG we construct at container start holding
  credentials for BOTH registries. The NGC key comes from a Snowflake SECRET, so
  it is never written to the stage, the Dockerfile, or shell history.

Prerequisites (sql-setup/1-setup.sql plus the objects below):
  - COMPUTE POOL     NIM_BUILD_POOL         CPU pool, no GPU needed to build
  - IMAGE REPOSITORY NIMS_DB.NV.NIM_GPU_IMAGES
  - STAGE            NIMS_DB.NV.BUILD_CTX   holds the build context
  - SECRET           NIMS_DB.NV.NGC_API_KEY GENERIC_STRING, the nvapi- key
  - EAI              NGC_REGISTRY_PULL_EAI  nvcr.io:443, authn.nvidia.com:443,
                                            api.ngc.nvidia.com:443
######################################################### */

USE ROLE SYSADMIN;
USE SCHEMA NIMS_DB.NV;

/* ---------------------------------------------------------------------------
Build pool. GEN_X64_G2_32 (28 vCPU / 116 GiB / 93 GiB node storage) so the 98
layers decompress in parallel. Every SPCS instance family caps node storage at
93 GiB, which is the real constraint on how large an image can be mirrored:
genmol:2.0 is 9.44 GiB compressed and roughly 30 GiB unpacked, so it fits.
--------------------------------------------------------------------------- */
CREATE COMPUTE POOL IF NOT EXISTS NIM_BUILD_POOL
  MIN_NODES = 1 MAX_NODES = 1
  INSTANCE_FAMILY = GEN_X64_G2_32
  AUTO_RESUME = TRUE
  AUTO_SUSPEND_SECS = 600
  COMMENT = 'CPU pool for SPCS image builds (mirroring NIM images without local Docker)';

ALTER COMPUTE POOL NIM_BUILD_POOL RESUME IF SUSPENDED;

/* ---------------------------------------------------------------------------
Egress. An image PULL from nvcr.io only needs the registry and its auth hosts,
so it can be scoped properly. This is a real difference from the NIM runtime:
the container's model weight download redirects to NGC CDN hosts that are not
enumerable, which is why nims_db.nv.nim_allow_all_egress exists in 1-setup.sql.
Mirroring the image up front does not require that open rule.
--------------------------------------------------------------------------- */
CREATE OR REPLACE NETWORK RULE NIMS_DB.NV.NGC_REGISTRY_PULL
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('nvcr.io:443', 'layers.nvcr.io:443', 'authn.nvidia.com:443', 'api.ngc.nvidia.com:443');

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION NGC_REGISTRY_PULL_EAI
  ALLOWED_NETWORK_RULES = (NIMS_DB.NV.NGC_REGISTRY_PULL)
  ENABLED = TRUE;

CREATE STAGE IF NOT EXISTS NIMS_DB.NV.BUILD_CTX
  DIRECTORY = (ENABLE = TRUE)
  COMMENT = 'Build contexts for SPCS image builds';

/* ---------------------------------------------------------------------------
Secret. Create once, out of band, so the key does not sit in this file:

  CREATE OR REPLACE SECRET NIMS_DB.NV.NGC_API_KEY
    TYPE = GENERIC_STRING
    SECRET_STRING = 'nvapi-...';

Build context: build/genmol/Dockerfile is a single line,
`FROM nvcr.io/nim/nvidia/genmol:2.0`. Upload it with:

  snow stage copy build/genmol/Dockerfile @NIMS_DB.NV.BUILD_CTX/genmol/ --overwrite
--------------------------------------------------------------------------- */

/* ---------------------------------------------------------------------------
The build job.

IMAGE_REGISTRY_URL must use the in-cluster registry hostname
`registry-local.snowflakecomputing.com`, not the external `registry.` hostname
that SHOW IMAGE REPOSITORIES returns.
--------------------------------------------------------------------------- */
EXECUTE JOB SERVICE
  IN COMPUTE POOL NIM_BUILD_POOL
  NAME = NIMS_DB.NV.MIRROR_GENMOL
  EXTERNAL_ACCESS_INTEGRATIONS = (NGC_REGISTRY_PULL_EAI)
  FROM SPECIFICATION $$
spec:
  containers:
    - name: main
      image: /snowflake/images/snowflake_images/sf-image-build:0.0.1
      env:
        IMAGE_REGISTRY_URL: sfsenorthamerica-cguzman_aws_us_west_2.registry-local.snowflakecomputing.com/nims_db/nv/nim_gpu_images
        IMAGE_NAME: genmol
        IMAGE_TAG: "2.0"
        BUILD_CONTEXT: /app
        PUSH_IMAGE: "true"
        DOCKER_CONFIG: /tmp/dockercfg
        SOURCE_REGISTRY: nvcr.io
        SOURCE_REGISTRY_USER: $oauthtoken
      secrets:
        - snowflakeSecret: NIMS_DB.NV.NGC_API_KEY
          secretKeyRef: SECRET_STRING
          envVarName: SOURCE_REGISTRY_TOKEN
      volumeMounts:
        - name: code-volume
          mountPath: /app
      resources:
        requests:
          cpu: 8
          memory: 32Gi
        limits:
          cpu: 24
          memory: 96Gi
      command:
        - /bin/sh
      args:
        - -c
        - |
          set -eu

          # The Snowflake registry credential the wrapper would normally write to
          # $HOME/.docker/config.json. Reproduced here because setting DOCKER_CONFIG
          # makes BuildKit read our file instead of the wrapper's.
          SF_TOKEN=$(tr -d '\n\r' < /snowflake/session/token)
          SF_HOST=$(echo "$IMAGE_REGISTRY_URL" | cut -d/ -f1)
          SF_AUTH=$(printf '%s:%s' '0auth2accesstoken' "$SF_TOKEN" | base64 | tr -d '\n')
          SRC_AUTH=$(printf '%s:%s' "$SOURCE_REGISTRY_USER" "$SOURCE_REGISTRY_TOKEN" | base64 | tr -d '\n')

          mkdir -p "$DOCKER_CONFIG"
          umask 077
          printf '{"auths":{"%s":{"auth":"%s"},"%s":{"auth":"%s"}}}\n' \
            "$SF_HOST" "$SF_AUTH" "$SOURCE_REGISTRY" "$SRC_AUTH" \
            > "$DOCKER_CONFIG/config.json"

          echo "registries configured: $SF_HOST, $SOURCE_REGISTRY"
          unset SOURCE_REGISTRY_TOKEN SRC_AUTH SF_TOKEN SF_AUTH

          exec /usr/local/bin/image-builder
  volumes:
    - name: code-volume
      source: stage
      stageConfig:
        name: "@NIMS_DB.NV.BUILD_CTX/genmol"
      uid: 65532
  $$;

/* ---------------------------------------------------------------------------
Verify, then suspend the pool so it stops billing.
--------------------------------------------------------------------------- */
SHOW IMAGES IN IMAGE REPOSITORY NIMS_DB.NV.NIM_GPU_IMAGES;

ALTER COMPUTE POOL NIM_BUILD_POOL SUSPEND;
