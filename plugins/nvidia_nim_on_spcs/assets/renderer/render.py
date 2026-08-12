#!/usr/bin/env python3
"""Deterministic renderer for the NVIDIA NIM on SPCS plugin.

Reads build_manifest.yaml + a per-account config.json and writes one file per
object. No database access -- this is a pure text transform. The skills execute
the rendered SQL with `snow sql -f`.

Same lineage as the DICOM Imaging Intelligence renderer (which in turn came from
the Pharmacy Cost Command Center renderer with the conformance-resolution layer
removed). This plugin owns everything it creates, so a template only needs to
resolve where objects go, what they are named, and which container registry
hostnames this account uses.

The one addition over the DICOM renderer is `--set key=value`. Two values cannot
live in config.json because they are properties of the ACCOUNT, not of the
deployment: the organization name and account name, which together form the
image registry hostnames. The skills resolve them with a query and pass them in,
which keeps config.json portable across accounts.

Template helpers exposed as Jinja globals:
  cfg                  -> the whole config dict (use cfg.<key>)
  target(object_name)  -> TARGET_DATABASE.TARGET_SCHEMA.OBJECT_NAME
  db / schema          -> cfg.target_database / cfg.target_schema
  warehouse            -> cfg.warehouse
  owner_role           -> cfg.owner_role
  nims                 -> only the entries of cfg.nims with enabled = true
  repo_path            -> "<db>/<schema>/<repo>" lowercased, as the registry wants
  push_host            -> <org>-<account>.registry-local.snowflakecomputing.com
  pull_host            -> <org>-<account>.registry.snowflakecomputing.com
  mirrored_image(nim)  -> the pull URL a CREATE SERVICE spec should reference

Why two hostnames: the image build job runs INSIDE the account and must push to
`registry-local`, which is the in-cluster endpoint. Everything outside the
cluster -- a service specification, `docker pull` -- uses `registry`. Using the
wrong one is a confusing failure, so the templates never hand-write either.

StrictUndefined is deliberate: a template that references a config key which
config.json does not define fails HERE, at render time, instead of emitting
"None" into a CREATE statement and failing halfway through a deploy.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Keys every render needs. Checked up front so a missing one produces a clear
# message instead of a Jinja traceback from inside a template. `org` and
# `account` are supplied by the skills via --set, not by config.json.
REQUIRED_KEYS = [
    "target_database",
    "target_schema",
    "warehouse",
    "owner_role",
    "image_repository",
    "org",
    "account",
]


def load_config(path, overrides):
    with open(path) as fh:
        try:
            cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            sys.exit(
                f"FAILED: {path} is not valid JSON ({exc}).\n"
                "config.json is read as strict JSON - it may not contain comments."
            )

    for item in overrides or []:
        if "=" not in item:
            sys.exit(f"FAILED: --set expects key=value, got '{item}'.")
        key, value = item.split("=", 1)
        cfg[key.strip()] = value.strip()

    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        sys.exit(
            f"FAILED: missing required key(s): {', '.join(missing)}.\n"
            "Copy assets/config.sample.json to config.json and fill it in. "
            "`org` and `account` come from --set org=... --set account=... ; the "
            "skills resolve them with:\n"
            "  snow sql -q \"select current_organization_name(), current_account_name()\""
        )

    enabled = [n for n in cfg.get("nims", []) if n.get("enabled")]
    if not enabled:
        sys.exit(
            "FAILED: no NIM in config.json has \"enabled\": true.\n"
            "Nothing would be mirrored or deployed."
        )

    # A duplicate key would silently overwrite another NIM's mirrored image.
    keys = [n["key"] for n in enabled]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        sys.exit(f"FAILED: duplicate NIM key(s) in config.json: {', '.join(sorted(dupes))}.")

    # Weight-cache values that CREATE SERVICE would only reject after the image
    # has been pulled and a GPU node provisioned. Fail here instead.
    for n in enabled:
        if not n.get("weight_cache"):
            continue
        need = ["weight_cache_size_gi", "weight_cache_path"]
        absent = [k for k in need if not n.get(k)]
        if absent:
            sys.exit(
                f"FAILED: NIM '{n['key']}' has weight_cache = true but is missing "
                f"{', '.join(absent)}."
            )
        size = n["weight_cache_size_gi"]
        if not isinstance(size, int) or not 1 <= size <= 65536:
            sys.exit(
                f"FAILED: NIM '{n['key']}' weight_cache_size_gi must be an integer "
                f"between 1 and 65536 (AWS), got {size!r}. The value is written with a "
                "Gi suffix and cannot be changed after the service is created."
            )
        if not str(n["weight_cache_path"]).startswith("/"):
            sys.exit(
                f"FAILED: NIM '{n['key']}' weight_cache_path must be an absolute "
                f"path, got {n['weight_cache_path']!r}."
            )
        # AWS caps throughput at 1 MiB/s per 4 IOPS, and the plugin does not set
        # iops, so the default 3000 applies -> 750 MiB/s ceiling.
        tp = n.get("weight_cache_throughput")
        if tp and (not isinstance(tp, int) or not 125 <= tp <= 750):
            sys.exit(
                f"FAILED: NIM '{n['key']}' weight_cache_throughput must be an integer "
                f"between 125 and 750, got {tp!r}. On AWS the ceiling is iops/4, and "
                "this plugin leaves iops at the 3000 default."
            )

    return cfg


def build_env(templates_dir, config):
    db = config["target_database"]
    schema = config["target_schema"]
    repo = config["image_repository"]
    org = config["org"].lower()
    # Account names routinely contain underscores (PS_CG_BC_SANDBOX_AWS_EAST) and
    # underscores are not legal in DNS hostnames, so Snowflake renders them as
    # hyphens. Match what SHOW IMAGE REPOSITORIES reports exactly rather than
    # lowercasing and hoping - `snow spcs service build-image` itself gets this
    # wrong and only works because the platform tolerates the underscore form.
    account = config["account"].lower().replace("_", "-")

    # The registry path segment is always lowercase, regardless of how the
    # Snowflake identifiers are cased.
    repo_path = f"{db}/{schema}/{repo}".lower()
    push_host = f"{org}-{account}.registry-local.snowflakecomputing.com"
    pull_host = f"{org}-{account}.registry.snowflakecomputing.com"

    def target(obj):
        return f"{db}.{schema}.{obj}"

    def mirrored_image(nim):
        return f"{pull_host}/{repo_path}/{nim['key']}:{nim['tag']}"

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.globals.update(
        cfg=config,
        target=target,
        db=db,
        schema=schema,
        warehouse=config["warehouse"],
        owner_role=config["owner_role"],
        nims=[n for n in config.get("nims", []) if n.get("enabled")],
        repo_path=repo_path,
        push_host=push_host,
        pull_host=pull_host,
        mirrored_image=mirrored_image,
    )
    return env


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--templates", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--set", dest="overrides", nargs="*",
                    help="key=value pairs merged over config.json (org, account)")
    ap.add_argument("--only", nargs="*", help="render only these object_names")
    ap.add_argument("--phase", nargs="*", help="render only these phases")
    a = ap.parse_args()

    config = load_config(a.config, a.overrides)
    env = build_env(a.templates, config)
    manifest = yaml.safe_load(open(a.manifest))

    os.makedirs(a.out, exist_ok=True)
    rendered = []
    skipped = []
    for obj in manifest["objects"]:
        name = obj["object_name"]
        if a.only and name not in a.only:
            continue
        if a.phase and obj.get("phase") not in a.phase:
            continue
        gate = obj.get("enabled_if")
        if gate and not config.get(gate):
            skipped.append({"object": name, "reason": f"{gate} is false"})
            continue

        tmpl = env.get_template(obj["template"])
        sql = tmpl.render(obj=obj, params=obj.get("params", {}))
        out_name = obj.get("out_file", f"{name}.sql")
        outp = Path(a.out) / out_name
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(sql)
        rendered.append(
            {
                "object": name,
                "phase": obj.get("phase"),
                "role": obj.get("role"),
                "path": str(outp),
            }
        )

    print(json.dumps({"rendered": rendered, "skipped": skipped}, indent=2))


if __name__ == "__main__":
    main()
