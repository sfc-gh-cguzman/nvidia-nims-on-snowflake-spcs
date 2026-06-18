# Running NVIDIA NIM Containers on SPCS

## Why Use NVIDIA NIM Containers

NIM (NVIDIA Inference Microservices) packages optimized model inference into pre-built containers with:

1. **Optimized inference** — TensorRT-LLM, vLLM, or Triton backends tuned per-model and GPU arch. You get near-peak throughput without hand-tuning batch sizes, KV cache, or quantization configs.

2. **API standardization** — Every NIM exposes an OpenAI-compatible `/v1/completions` or `/v1/chat/completions` endpoint (plus model-specific endpoints for embeddings, reranking, etc.). Swap models without changing app code.

3. **Enterprise model catalog** — Access to Llama 3.1, Mistral, Nemotron, BioNeMo models, medical imaging (MONAI-based NIMs), drug discovery, protein folding, speech (Riva), and more — all with NVIDIA AI Enterprise support.

4. **Quantization built-in** — FP8, INT8, INT4 (AWQ/GPTQ) profiles ship with the container. No manual quantization pipeline.

5. **Multi-GPU / tensor parallelism** — Larger models (70B+) auto-shard across GPUs with TP/PP configs baked in.

### Common use cases

- **Private LLM inference** — Run Llama 3.1 70B or Mistral inside Snowflake's security perimeter (no data leaves the account)
- **Domain-specific models** — BioNeMo NIMs for molecular generation, protein structure, ADMET prediction
- **Medical imaging** — MONAI-based NIMs for segmentation, registration, classification (radiology, pathology)
- **Embeddings & reranking** — NV-Embed-v2 or NV-RerankQA for RAG pipelines colocated with your data
- **Speech/NLP** — Riva NIMs for ASR/TTS in regulated environments
- **Guardrails** — NeMo Guardrails NIM for content filtering on top of any LLM

## How to Run a NIM on SPCS

### Prerequisites

- SPCS compute pool with GPU nodes (e.g., `GPU_NV_M` for A10G, `GPU_NV_L` for A100)
- NGC API key (from [build.nvidia.com](https://build.nvidia.com)) stored as a Snowflake secret
- Image repository in your account
- External access integration for pulling from `nvcr.io`

### Step-by-step

**1. Create the network rule and external access integration**

```sql
CREATE OR REPLACE NETWORK RULE ngc_network_rule
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('nvcr.io', 'authn.nvidia.com', 'helm.ngc.nvidia.com');

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION ngc_access_integration
  ALLOWED_NETWORK_RULES = (ngc_network_rule)
  ENABLED = TRUE;
```

**2. Store your NGC API key as a secret**

```sql
CREATE OR REPLACE SECRET ngc_api_key
  TYPE = GENERIC_STRING
  SECRET_STRING = '<your-ngc-api-key>';
```

**3. Create an image repository (if you want to cache the image)**

```sql
CREATE OR REPLACE IMAGE REPOSITORY my_db.my_schema.nim_images;
```

You can either pull directly from `nvcr.io` at service start (simpler) or pre-push the image to your repo (faster cold starts).

**4. Create a compute pool with GPUs**

```sql
CREATE COMPUTE POOL IF NOT EXISTS nim_gpu_pool
  MIN_NODES = 1
  MAX_NODES = 1
  INSTANCE_FAMILY = GPU_NV_M;  -- A10G (24GB); use GPU_NV_L for A100
```

**5. Write the service spec YAML**

Example for `meta/llama-3.1-8b-instruct` NIM:

```yaml
spec:
  containers:
    - name: nim-llm
      image: nvcr.io/nim/meta/llama-3.1-8b-instruct:latest
      env:
        NGC_API_KEY:
          type: secret
          value: ngc_api_key
        NIM_MAX_MODEL_LEN: "4096"
      resources:
        requests:
          nvidia.com/gpu: 1
          memory: 24Gi
        limits:
          nvidia.com/gpu: 1
          memory: 24Gi
      readinessProbe:
        httpGet:
          path: /v1/health/ready
          port: 8000
        initialDelaySeconds: 120
        periodSeconds: 10
  endpoints:
    - name: inference
      port: 8000
      public: false  # set true if you need external access
```

**6. Create the service**

```sql
CREATE SERVICE my_db.my_schema.nim_llama31_8b
  IN COMPUTE POOL nim_gpu_pool
  FROM @my_db.my_schema.specs
  SPECIFICATION_FILE = 'nim-llama31-8b.yaml'
  EXTERNAL_ACCESS_INTEGRATIONS = (ngc_access_integration)
  MIN_INSTANCES = 1
  MAX_INSTANCES = 1;
```

**7. Query the service**

From a Snowflake SQL context (e.g., a UDF or stored procedure):

```sql
-- Check status
SELECT SYSTEM$GET_SERVICE_STATUS('my_db.my_schema.nim_llama31_8b');

-- Call the endpoint via service function
CREATE OR REPLACE FUNCTION llm_complete(prompt VARCHAR)
RETURNS VARCHAR
SERVICE = my_db.my_schema.nim_llama31_8b
ENDPOINT = inference
AS '/v1/chat/completions';
```

Or from Python (Snowpark / stored proc):

```python
import requests

resp = requests.post(
    "http://nim-llama31-8b:8000/v1/chat/completions",
    json={
        "model": "meta/llama-3.1-8b-instruct",
        "messages": [{"role": "user", "content": "Summarize this clinical note..."}],
        "max_tokens": 512
    }
)
```

### Key considerations

| Concern | Guidance |
|---------|----------|
| Cold start | NIMs download model weights on first boot (~2-10 min depending on model size). Pre-push to image repo or use persistent volumes to cache. |
| GPU sizing | 8B models fit on 1x A10G (24GB). 70B models need 4x A10G or 1-2x A100 (80GB) with TP=4 or TP=2. |
| Cost | Compute pool bills per-node-hour while running. Suspend the pool when idle if latency on restart is acceptable. |
| Security | Data stays inside Snowflake's perimeter. No inference traffic leaves the VPC unless you explicitly open egress. |
| Licensing | NIM containers require NVIDIA AI Enterprise (NVAIE) entitlement. The NGC API key enforces this. |

### Customer relevance (HCLS)

- **Edwards**: MONAI NIMs for 3D segmentation/registration could complement the existing MONAI+Ray pipeline — pre-built, optimized, and easier to operationalize than custom containers.
- **ISRG**: Private LLM inference for RepEdge (keep sales conversation data in-perimeter) or manufacturing QA classification.
- **Doximity**: medCPT / clinical NER models could potentially ship as custom NIMs if NVIDIA supports BYOM NIM packaging, or use NV-Embed NIM for the embedding layer alongside their existing HF models.
