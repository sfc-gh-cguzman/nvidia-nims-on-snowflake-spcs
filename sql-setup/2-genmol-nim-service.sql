-- SPCS setup for NVIDIA NIM GenMol 2.0 (fragment-based molecular generation)
-- Image: nvcr.io/nim/nvidia/genmol:2.0.0
-- Endpoint: /generate (molecular generation), /v1/health/ready (readiness)
-- GPU: 1x A10G (24GB) sufficient for this model
-- Co-authored with CoCo

USE SCHEMA nims_db.nv;

----------------------------------------------------------------------
-- 1. Push the image to your Snowflake image repository
--    (run from a machine with Docker + NGC access)
----------------------------------------------------------------------
-- docker login sfsenorthamerica-cguzman-aws-us-west-2.registry.snowflakecomputing.com -u <user>
-- docker pull nvcr.io/nim/nvidia/genmol:2.0.0
-- docker tag nvcr.io/nim/nvidia/genmol:2.0.0 \
--   sfsenorthamerica-cguzman-aws-us-west-2.registry.snowflakecomputing.com/nims_db/nv/nim_gpu_images/genmol:2.0.0
-- docker push sfsenorthamerica-cguzman-aws-us-west-2.registry.snowflakecomputing.com/nims_db/nv/nim_gpu_images/genmol:2.0.0

----------------------------------------------------------------------
-- 2. Verify image is available
----------------------------------------------------------------------
SHOW IMAGES IN IMAGE REPOSITORY nims_db.nv.nim_gpu_images;

----------------------------------------------------------------------
-- 3. Create the GenMol SPCS service
----------------------------------------------------------------------
CREATE SERVICE nims_db.nv.nim_genmol_svc
  IN COMPUTE POOL nim_gpu_pool
  FROM SPECIFICATION $$
spec:
  containers:
    - name: genmol
      image: sfsenorthamerica-cguzman-aws-us-west-2.registry.snowflakecomputing.com/nims_db/nv/nim_gpu_images/genmol:latest
      env:
        NIM_HTTP_API_PORT: "8000"
        NIM_LOG_LEVEL: "INFO"
      secrets:
        - snowflakeSecret: nims_db.nv.ngc_api_key
          secretKeyRef: secret_string
          envVarName: NGC_API_KEY
      resources:
        requests:
          nvidia.com/gpu: 1
          memory: 16Gi
        limits:
          nvidia.com/gpu: 1
          memory: 24Gi
      readinessProbe:
        port: 8000
        path: /v1/health/ready
  endpoints:
    - name: generate
      port: 8000
      public: true
$$
  EXTERNAL_ACCESS_INTEGRATIONS = (NIM_OPEN_EGRESS);

----------------------------------------------------------------------
-- 4. Check service status
----------------------------------------------------------------------
DESCRIBE SERVICE nims_db.nv.nim_genmol_svc;
SELECT SYSTEM$GET_SERVICE_STATUS('nims_db.nv.nim_genmol_svc');
SHOW ENDPOINTS IN SERVICE nims_db.nv.nim_genmol_svc;

----------------------------------------------------------------------
-- 5. View logs (debug startup issues)
----------------------------------------------------------------------
-- SELECT SYSTEM$GET_SERVICE_LOGS('nims_db.nv.nim_genmol_svc', 0, 'genmol', 100);

----------------------------------------------------------------------
-- 6. Test the /generate endpoint (de novo generation)
----------------------------------------------------------------------
-- Once the endpoint URL is available from SHOW ENDPOINTS:
--
-- curl -X POST https://<endpoint-url>/generate \
--   -H "Content-Type: application/json" \
--   -H "Authorization: Snowflake Token=\"<token>\"" \
--   -d '{
--     "num_molecules": 10,
--     "temperature": 1.0,
--     "noise": 1.0,
--     "scoring": "QED",
--     "unique": true
--   }'

----------------------------------------------------------------------
-- 7. Test conditioned generation (motif extension)
----------------------------------------------------------------------
-- curl -X POST https://<endpoint-url>/generate \
--   -H "Content-Type: application/json" \
--   -H "Authorization: Snowflake Token=\"<token>\"" \
--   -d '{
--     "smiles": "[C@H]1O[C@@H](CO)[C@H](O)[C@@H]1O.[*{15-15}]",
--     "num_molecules": 5,
--     "temperature": 1.0,
--     "noise": 1.0,
--     "scoring": "QED",
--     "filter": true
--   }'

----------------------------------------------------------------------
-- 8. Cleanup
----------------------------------------------------------------------
-- DROP SERVICE nims_db.nv.nim_genmol_svc;
