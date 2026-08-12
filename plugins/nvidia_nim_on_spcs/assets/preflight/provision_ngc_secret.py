#!/usr/bin/env python3
"""Prompt for an NGC API key, validate it against the registry, create the Snowflake secret.

Run this yourself. Do NOT let an agent run it and do NOT paste a key into a chat, a
prompt, or a command line:

  - Anything typed to an assistant is persisted to conversation history on disk in
    plaintext, permanently.
  - argv is world-readable via `ps`, so a key passed as a flag leaks to any local
    process.

This script is the safe path: the key is read from a no-echo TTY prompt into memory,
used, and discarded. It is never printed, never written to disk, and never passed as an
argument. Output is a handful of status lines that are safe to show anyone.

Why getpass rather than a shell `read`: the hidden-read flag differs between shells.
`read -rsp 'prompt' VAR` is bash; under zsh `-p` means "read from coprocess", so that
same line fails with `read: -p: no coprocess` and silently leaves the variable EMPTY -
which then creates an empty secret and surfaces much later as an auth error. getpass
behaves identically on both.

The plaintext key is needed on this machine exactly once, for validation. After the
secret exists, every consumer (the mirror job, the NIM services) reads it inside
Snowflake via `secrets:`/`secretKeyRef` and nothing outside Snowflake needs it again.

Usage:
    python3 provision_ngc_secret.py --connection <name> [--config <path>] [--replace]

Exit codes:
    0  secret created (or already present and left alone)
    1  preflight failed, or the secret exists and --replace was not given
    2  usage error
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_ngc_key import (  # noqa: E402
    check_key_valid,
    check_tag,
    registry_token,
    repo_from_image,
)


def die(msg, code=2):
    print(f"FAILED: {msg}", file=sys.stderr)
    sys.exit(code)


def sql_literal(value):
    """Escape for a single-quoted Snowflake string literal.

    Backslash first, then quote, or the quote's escape would itself be re-escaped.
    NGC keys are alphanumeric in practice, but a mistyped paste should break the
    statement rather than change its meaning.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def prompt_for_key():
    """Read the key twice from a no-echo prompt and require the two to match.

    Double entry is not about typos. `sys.stdin.isatty()` returns True inside an
    agent's pseudo-terminal, and in that context getpass does not block - it returns
    whatever stale bytes are sitting in the pty buffer. Measured: a getpass call in an
    agent shell captured 13 characters of leftover input with no human involved. So
    isatty alone cannot distinguish a person from an automation harness.

    Requiring the same value twice closes it. Buffered junk does not repeat itself,
    and a human typing a key twice will match.
    """
    if not sys.stdin.isatty():
        die("stdin is not a TTY, so this cannot prompt.\n"
            "  Run it yourself in a terminal. Do not run it through an agent.")
    first = getpass.getpass("NGC API key (nvapi-..., input hidden): ").strip()
    if not first:
        die("nothing captured - no secret was created.", 1)
    second = getpass.getpass("Re-enter the same key to confirm: ").strip()
    if first != second:
        die("the two entries did not match, so nothing was created.\n"
            "  If you did not type twice, this shell cannot prompt safely - run the\n"
            "  script directly in a terminal rather than through a tool or an agent.", 1)
    return first


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--connection", required=True, help="connections.toml entry name")
    ap.add_argument("--config", default=None, help="plugin config.json (default: ../../config.json)")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite the secret if it already exists")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else \
        Path(__file__).resolve().parents[2] / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception as exc:  # noqa: BLE001
        die(f"could not read config {cfg_path}: {exc}")

    nims = [n for n in cfg.get("nims", []) if n.get("enabled")]
    if not nims:
        die('no NIM in config.json has "enabled": true', 1)

    fq = f"{cfg['target_database']}.{cfg['target_schema']}.{cfg['ngc_secret']}"

    try:
        import snowflake.connector
    except ImportError:
        die("snowflake-connector-python is not importable by this interpreter.\n"
            "  Check with: python -c 'import snowflake.connector'\n"
            "  A system python3 often lacks it while a conda/venv python has it.")

    # Prompt before connecting: no point holding a session open while a human types.
    # An env var is honoured so this can run unattended, but it is not advertised in
    # the skill because a prompt is the safer default.
    key = os.environ.get("NGC_API_KEY", "").strip()
    if key:
        print("using NGC_API_KEY from the environment (not prompting)")
    else:
        key = prompt_for_key()

    if not key:
        die("empty key - nothing was captured, so no secret was created.", 1)
    if not key.startswith("nvapi-"):
        die("key does not start with 'nvapi-'. Personal NGC API keys do.", 1)

    # --- Preflight. Never create a secret from an unvalidated key: a bad one does not
    # fail here, it fails in the build phase with a bare 403 after a pool has started.
    ok, detail = check_key_valid(key)
    print(f"[{'PASS' if ok else 'FAIL'}] key            {detail}")
    if not ok:
        die("key did not validate against NGC. Secret NOT created.", 1)

    problems = []
    for nim in nims:
        repo, tag = repo_from_image(nim["image"]), str(nim["tag"])
        token, err = registry_token(key, repo)
        if not token:
            print(f"[FAIL] {nim['key']:<14} {repo}:{tag} - {err}")
            problems.append(f"{nim['key']}: {err}")
            continue
        tag_ok, tag_detail, real = check_tag(token, repo, tag)
        print(f"[{'PASS' if tag_ok else 'FAIL'}] {nim['key']:<14} "
              f"entitled to {repo}, {tag_detail}")
        if not tag_ok:
            print(f"                      available tags: "
                  f"{', '.join(real[-8:]) if real else 'none visible'}")
            problems.append(f"{nim['key']}: {tag_detail}")

    if problems:
        print("\nPREFLIGHT FAILED - secret NOT created:")
        for p in problems:
            print(f"  - {p}")
        print("\nEntitlement failure and wrong repo name are indistinguishable (both\n"
              "403). Confirm the model at ngc.nvidia.com and that your org holds NVAIE\n"
              "entitlement for it.")
        sys.exit(1)

    # --- Create the secret. Built as a literal because CREATE SECRET does not accept
    # bind parameters. Verified safe against leakage: Snowflake redacts SECRET_STRING
    # in QUERY_HISTORY, storing it as decorative glyphs rather than the value.
    with snowflake.connector.connect(connection_name=args.connection) as conn:
        cur = conn.cursor()
        cur.execute(f"SHOW SECRETS LIKE '{cfg['ngc_secret']}' IN SCHEMA "
                    f"{cfg['target_database']}.{cfg['target_schema']}")
        exists = bool(cur.fetchall())
        if exists and not args.replace:
            print(f"\n[SKIP] {fq} already exists. Left untouched.")
            print("       Re-run with --replace to rotate it.")
            return 1
        verb = "CREATE OR REPLACE" if exists else "CREATE"
        cur.execute(
            f"{verb} SECRET {fq} TYPE = GENERIC_STRING "
            f"SECRET_STRING = '{sql_literal(key)}' "
            f"COMMENT = 'NGC API key for pulling NVIDIA NIM images'"
        )
        del key
        print(f"\n[OK]   {'rotated' if exists else 'created'} {fq}")
        cur.execute(f"SHOW SECRETS LIKE '{cfg['ngc_secret']}' IN SCHEMA "
                    f"{cfg['target_database']}.{cfg['target_schema']}")
        for row in cur.fetchall():
            print(f"       name={row[1]}  type={row[5]}  created={row[0]}")
    print("\nThe plaintext key is no longer needed outside Snowflake.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
