/* ######################################################### 
ASSIGN INTEGRATION AND COMPUTE POOL PRIVILEGES TO SYSADMIN ROLE
######################################################### */
use role accountadmin;
grant create integration on account to role sysadmin;
grant create compute pool on account to role sysadmin;


/* ######################################################### 
CREATE DATABASE AND SCHEMA
######################################################### */
use role sysadmin;

create or alter database docs_db;
create or alter schema docs_db.main;


/* ######################################################### 
SPCS Setup
######################################################### */

-- SPCS setup for NVIDIA NIM (Llama 3.1 8B) with compute pool and service creation

create or alter database nims_db;
create or alter schema nv;

use schema nims_db.nv;

CREATE COMPUTE POOL IF NOT EXISTS nim_gpu_pool
  MIN_NODES = 1
  MAX_NODES = 2
  INSTANCE_FAMILY = GPU_NV_M;  -- A10G (24GB); use GPU_NV_L for A100

ALTER COMPUTE POOL nim_gpu_pool RESUME;

CREATE OR REPLACE IMAGE REPOSITORY nim_gpu_images;

show image repositories;



----------------------------------------------------------------------
-- 1. Network rule + external access integration (NGC auth at runtime)
----------------------------------------------------------------------
-- Network rule: open egress for NGC CDN model weight downloads
-- (NGC API redirects to CDN/S3 hosts not predictable in advance)
CREATE OR REPLACE NETWORK RULE nims_db.nv.nim_allow_all_egress
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('0.0.0.0:443', '0.0.0.0:80');

-- External access integration referencing the rule
CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION nim_open_egress
  ALLOWED_NETWORK_RULES = (nims_db.nv.nim_allow_all_egress)
  ENABLED = TRUE;

-- CREATE OR REPLACE SECRET ngc_api_key
--   TYPE = GENERIC_STRING
--   SECRET_STRING = 'REPLACE WITH YOUR nvapi-KEY';