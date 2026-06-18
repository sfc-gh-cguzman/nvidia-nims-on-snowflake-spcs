USE SCHEMA NIMS_DB.NV;

CREATE SERVICE nims_db.nv.nim_llama31_8b_V2
  IN COMPUTE POOL GPU_ML_M_POOL
  FROM SPECIFICATION $$
spec:
  containers:
    - name: nim-llm
      image: sfsenorthamerica-cguzman-aws-us-west-2.registry.snowflakecomputing.com/nims_db/nv/nim_gpu_images/nims-llama-3.1-8b-instruct:linux
      env:
        NIM_MAX_MODEL_LEN: "4096"
        NIM_LOG_LEVEL: "DEBUG"
      secrets:
        - snowflakeSecret: nims_db.nv.ngc_api_key
          secretKeyRef: secret_string
          envVarName: NGC_API_KEY
      resources:
        requests:
          nvidia.com/gpu: 1
          memory: 24Gi
        limits:
          nvidia.com/gpu: 1
          memory: 24Gi
      readinessProbe:
        port: 8000
        path: /v1/health/ready
  endpoints:
    - name: inference
      port: 8000
      public: true
$$
  EXTERNAL_ACCESS_INTEGRATIONS = (nim_open_egress);

--describe service nims_db.nv.nim_llama31_8b;
describe service nims_db.nv.nim_llama31_8b_V2;

show endpoints in service nims_db.nv.nim_llama31_8b_V2;

--bnc42qqb-sfsenorthamerica-cguzman-aws-us-west-2.snowflakecomputing.app

-- Check status
SELECT SYSTEM$GET_SERVICE_STATUS('nims_db.nv.nim_llama31_8b');
