# Mirror the NVIDIA NIM image into the Snowflake image repository with no local Docker.
# BuildKit resolves this base from nvcr.io, so the build job needs:
#   1. egress to nvcr.io (external access integration)
#   2. nvcr.io credentials in $DOCKER_CONFIG/config.json
# Platform is not pinned here: the SPCS build node is linux/amd64, so the correct
# variant of the multi-arch manifest is selected automatically.
FROM nvcr.io/nim/nvidia/genmol:2.0
