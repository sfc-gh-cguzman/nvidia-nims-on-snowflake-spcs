snow spcs image-registry login -c <your snowflake connection>

docker pull --platform linux/amd64 nvcr.io/nim/nvidia/genmol:2.0
docker tag nvcr.io/nim/nvidia/genmol:2.0 <yourimage-rgistry/genmol>

docker push <yourimage-rgistry/genmol>