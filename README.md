# NVIDIA NIMs on Snowpark Container Services (SPCS)

Running NVIDIA Inference Microservices inside Snowflake's security perimeter for healthcare and life sciences workloads.

## What this repo demonstrates

Two NVIDIA NIM containers deployed as SPCS services, accessible via standard HTTP endpoints with Snowflake token-based authentication:

| Service | Model | Use case | GPU | Endpoint |
|---------|-------|----------|-----|----------|
| `nim_llama31_8b_v2` | Meta Llama 3.1 8B Instruct | General-purpose LLM (chat, summarization, extraction) | 1x A10G (24GB) | `/v1/chat/completions` |
| `nim_genmol_svc` | NVIDIA GenMol 2.0 | Fragment-based molecular generation for drug discovery | 1x A10G (24GB) | `/generate` |

## Why run NIMs on SPCS

### Data sovereignty and IP protection

Pharmaceutical and biotech companies operate under strict data governance. Proprietary compound libraries, clinical trial data, and patient information cannot leave the organization's control boundary. Running inference inside SPCS means:

- Molecular structures never traverse external networks
- LLM prompts containing sensitive clinical/regulatory text stay within Snowflake
- Audit trails via Snowflake ACCESS_HISTORY cover every inference call
- RBAC controls who can invoke each model endpoint

### Unified data + compute platform

Instead of building separate ML infrastructure alongside your data warehouse:

- Compound libraries, assay results, and clinical trial metadata live in Snowflake tables
- Generated molecules land directly back into tables for downstream scoring and filtering
- LLM-based extraction and summarization operate on data in place
- No ETL pipelines to move data to/from external inference services

### Enterprise GPU access without infrastructure overhead

- SPCS compute pools provide on-demand NVIDIA GPUs (A10G, A100, H100)
- No Kubernetes cluster management, driver updates, or capacity planning
- Auto-suspend when idle, resume on first request
- Cost tied directly to usage, not reserved capacity

## NVIDIA NIM value

NVIDIA Inference Microservices (NIMs) are production-optimized containers that wrap foundation models with:

- **Optimized inference runtimes** - TensorRT-LLM for LLMs, custom CUDA kernels for BioNeMo models
- **Standard API interfaces** - OpenAI-compatible for LLMs, domain-specific REST for scientific models
- **Enterprise support and CVE patching** - NVIDIA monitors and patches containers continuously
- **Model profile auto-selection** - NIM SDK detects available GPU hardware and loads the optimal model profile automatically
- **Built-in observability** - Health endpoints, Prometheus metrics, structured logging

For life sciences specifically, NVIDIA's BioNeMo NIM catalog includes models for:

- Molecular generation (GenMol)
- Protein structure prediction (ESMFold, AlphaFold2)
- Molecular docking (DiffDock)
- Protein language models (ESM-2)
- ADMET property prediction

Each of these can be deployed on SPCS using the same pattern shown in this repo.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Snowflake Account                                      │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │  SPCS Compute Pool (GPU_NV_M - A10G)             │  │
│  │                                                   │  │
│  │  ┌─────────────────┐  ┌─────────────────────┐    │  │
│  │  │  Llama 3.1 8B   │  │  GenMol 2.0         │    │  │
│  │  │  (NIM Container)│  │  (NIM Container)    │    │  │
│  │  │  Port 8000      │  │  Port 8000          │    │  │
│  │  └────────┬────────┘  └──────────┬──────────┘    │  │
│  │           │                       │               │  │
│  └───────────┼───────────────────────┼───────────────┘  │
│              │                       │                   │
│  ┌───────────┴───────────────────────┴───────────────┐  │
│  │  SPCS Ingress (public endpoints)                  │  │
│  │  Auth: Snowflake Token (PAT / OAuth)              │  │
│  └───────────┬───────────────────────┬───────────────┘  │
│              │                       │                   │
│  ┌───────────┴─────┐  ┌─────────────┴───────────────┐  │
│  │ Notebooks / Apps │  │  Snowflake Tables           │  │
│  │ (Python clients) │  │  (compound libs, results)   │  │
│  └─────────────────┘  └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Deployment pattern

All services follow the same 4-step pattern:

1. **Push image** - Pull from `nvcr.io`, tag for your Snowflake image repository, push
2. **Create service** - Inline YAML spec with GPU resource requests, NGC API key secret, readiness probe
3. **External access** - Network rule + integration allowing egress to NGC CDN for model weight downloads at startup
4. **Authenticate and call** - Snowflake PAT or OAuth token in the `Authorization` header

### Key infrastructure (shared across services)

```sql
-- Compute pool with A10G GPUs
CREATE COMPUTE POOL nim_gpu_pool
  MIN_NODES = 1 MAX_NODES = 1
  INSTANCE_FAMILY = GPU_NV_M;

-- NGC API key for model downloads
CREATE SECRET ngc_api_key
  TYPE = GENERIC_STRING
  SECRET_STRING = '<your-ngc-key>';

-- Open egress for NGC CDN (model weight downloads redirect to CDN hosts)
CREATE NETWORK RULE nim_allow_all_egress
  MODE = EGRESS TYPE = HOST_PORT
  VALUE_LIST = ('0.0.0.0:443', '0.0.0.0:80');

CREATE EXTERNAL ACCESS INTEGRATION nim_open_egress
  ALLOWED_NETWORK_RULES = (nim_allow_all_egress)
  ENABLED = TRUE;
```

## GenMol - molecular generation for drug discovery

GenMol is a masked diffusion model trained on SAFE (Sequential Attachment-based Fragment Embedding) representations. It generates novel molecules by iteratively unmasking token positions, with fine-grained control over what gets generated.

### Capabilities

| Task | Description | Input example |
|------|-------------|---------------|
| De novo generation | Random sampling of valid drug-like molecules | `null` (no SMILES input) |
| Motif extension | Add fragment to a specific attachment point | `[C@H]1O[C@@H](CO)...[*{15-15}]` |
| Scaffold decoration | Decorate a fixed core scaffold | Core SMILES with masked positions |
| Linker design | Connect two fragments with a bridge | Two fragments with attachment points |
| Superstructure generation | Extend molecule at any attachment point | SMILES without explicit mask position |

### Example output (de novo, scored by QED)

```json
{
  "status": "success",
  "molecules": [
    {"smiles": "CN(C1CCCCC1)S(=O)(=O)C[C@@H](O)c1ccc(Cl)cc1", "score": 0.902},
    {"smiles": "CCCCn1cc(N)c(C(=O)N2CCC(C3CC3)C2)n1", "score": 0.896},
    {"smiles": "COC1CCN(C(=O)C2CCN(c3ccc(Cl)cc3F)CC2)CC1", "score": 0.834}
  ]
}
```

QED (Quantitative Estimate of Drug-likeness) ranges from 0 to 1, with >0.7 generally considered drug-like.

## Llama 3.1 8B - general-purpose LLM

Deployed as an OpenAI-compatible chat completions endpoint. Use cases in life sciences:

- Summarizing clinical trial protocols and regulatory documents
- Extracting structured data from unstructured medical text
- Generating patient-friendly explanations of procedures
- Code generation for data pipeline development
- Internal Q&A over company knowledge bases

### Example call

```python
payload = {
    "model": "meta/llama-3.1-8b-instruct",
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarize the mechanism of action of pembrolizumab in 2 sentences."}
    ],
    "max_tokens": 256,
    "temperature": 0.7
}
```

## Files in this repo

```
specs/
  create-nim-service.sql      # Llama 3.1 8B service definition + infra setup
  genmol-nim-service.sql      # GenMol 2.0 service definition

nims_spcs.ipynb               # Llama 3.1 8B inference notebook
genmol_nims_spcs.ipynb        # GenMol inference notebook
```

## Extending this pattern

The same deployment pattern works for any NIM container. Candidates for HCLS workloads:

- **DiffDock** - Molecular docking (predict how a drug binds to a target protein)
- **ESM-2** - Protein embeddings for similarity search and classification
- **MolMIM** - Molecule optimization with controllable property generation
- **AlphaFold2** - Protein structure prediction

Each requires only: image push, service YAML with appropriate GPU/memory resources, and the shared NGC secret + egress integration.

## Operational notes

- **Startup time**: GenMol downloads ~500MB of model weights on first start (~4s on SPCS egress). Llama 3.1 8B downloads ~16GB (~2-3 min).
- **Cold start**: If the compute pool auto-suspends, first request triggers a resume + container start. Expect 3-5 minutes for Llama, under 1 minute for GenMol.
- **Scaling**: Increase `MAX_NODES` on the compute pool and add `MIN_INSTANCES`/`MAX_INSTANCES` on the service for horizontal scaling.
- **Cost**: GPU_NV_M (A10G) compute pools bill per-second while active. Suspend pools when not in use to avoid charges.
- **Monitoring**: `SYSTEM$GET_SERVICE_STATUS`, `SYSTEM$GET_SERVICE_LOGS`, and the NIM `/v1/metrics` endpoint (Prometheus format) for observability.
