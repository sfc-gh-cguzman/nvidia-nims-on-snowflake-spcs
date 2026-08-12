#!/usr/bin/env python3
"""Validate an NGC API key against the registry BEFORE creating a Snowflake secret.

Three failures this catches, all of which otherwise surface late and expensively:

  1. A bad or expired key. Without this check the first symptom is a mirror job
     failing with `403 Forbidden` from nvcr.io/proxy_auth, after a compute pool has
     started.
  2. A valid key with no entitlement to a specific NIM. NIM containers require NVAIE
     entitlement and it is granted per model, so a key that mirrors genmol may have
     no access to another NIM at all. Verified: an unentitled repo returns exactly
     the same 403 as a bad key, so entitlement cannot be inferred from one call.
  3. A tag that does not exist. Catches a typo or a withdrawn version before the
     build phase spends four minutes discovering it.

The key is read from the NGC_API_KEY environment variable, never from argv, because
argv is visible to any other process via `ps`. It is never printed, and never written
anywhere by this script.

Usage:
    NGC_API_KEY=nvapi-... python3 check_ngc_key.py --config /path/to/config.json

Exit codes:
    0  every check passed
    1  at least one check failed
    2  usage error (no key, unreadable config)

Scope note: this validates the KEY and the ENTITLEMENT from wherever you run it. It
does not validate that SPCS can reach NGC - that depends on the external access
integration, and a laptop behind a corporate proxy can easily differ from a compute
pool. The egress path is verified separately by the deploy itself.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
NGC_API = "https://api.ngc.nvidia.com"
REGISTRY = "https://nvcr.io"


def fail(msg, code=2):
    print(f"FAILED: {msg}", file=sys.stderr)
    sys.exit(code)


def repo_from_image(image):
    """nvcr.io/nim/nvidia/genmol -> nim/nvidia/genmol

    Drops a leading registry host. A host is identified by containing a dot or a
    colon, which is the same heuristic Docker uses.
    """
    parts = image.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
        parts = parts[1:]
    return "/".join(parts)


def check_key_valid(key):
    """GET /v2/users/me with the nvapi- key as a plain bearer token.

    Repo-independent, so it distinguishes a bad key from a missing entitlement.
    """
    req = urllib.request.Request(
        f"{NGC_API}/v2/users/me",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    try:
        body = json.load(urllib.request.urlopen(req, timeout=TIMEOUT))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from /v2/users/me"
    except Exception as exc:  # noqa: BLE001 - network shape varies
        return False, f"{type(exc).__name__} reaching {NGC_API}"
    # Report the username so the operator can confirm which NGC account this is.
    # Deliberately not the email address.
    name = (body.get("user") or {}).get("name") or "unknown"
    return True, f"valid, NGC user '{name}'"


def registry_token(key, repo):
    """Pull-scoped registry token. 200 means valid AND entitled to this repo."""
    basic = base64.b64encode(f"$oauthtoken:{key}".encode()).decode()
    url = f"{REGISTRY}/proxy_auth?" + urllib.parse.urlencode(
        {"account": "$oauthtoken", "scope": f"repository:{repo}:pull"}
    )
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + basic})
    try:
        return json.load(urllib.request.urlopen(req, timeout=TIMEOUT))["token"], None
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return None, "403 - key not entitled to this repo, or repo name is wrong"
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}"


def check_tag(token, repo, tag):
    req = urllib.request.Request(
        f"{REGISTRY}/v2/{repo}/tags/list", headers={"Authorization": "Bearer " + token}
    )
    try:
        tags = json.load(urllib.request.urlopen(req, timeout=TIMEOUT)).get("tags", [])
    except urllib.error.HTTPError as exc:
        return False, f"tags/list HTTP {exc.code}", []
    except Exception as exc:  # noqa: BLE001
        return False, f"tags/list {type(exc).__name__}", []
    # The tag list is mostly sbom/sig/vex attestation artifacts; drop them so the
    # "did you mean" suggestion is readable.
    real = [t for t in tags if not t.startswith("sha256-")]
    if tag in real:
        return True, f"tag '{tag}' exists", real
    return False, f"tag '{tag}' NOT found", real


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="path to the plugin config.json")
    args = ap.parse_args()

    key = os.environ.get("NGC_API_KEY", "").strip()
    if not key:
        fail(
            "NGC_API_KEY is not set.\n"
            "  Prefer provision_ngc_secret.py, which prompts safely and creates the\n"
            "  secret in one step. To set it by hand, note that the hidden-read flag\n"
            "  is not portable:\n"
            "    zsh:  read -rs \"?NGC API key: \"; export NGC_API_KEY=\"$REPLY\"\n"
            "    bash: read -rsp 'NGC API key: ' NGC_API_KEY; export NGC_API_KEY\n"
            "  Using the bash form under zsh fails with 'no coprocess' and silently\n"
            "  leaves the variable empty."
        )
    if not key.startswith("nvapi-"):
        fail("NGC_API_KEY does not start with 'nvapi-'. Personal NGC API keys do.", 1)

    try:
        cfg = json.load(open(args.config))
    except Exception as exc:  # noqa: BLE001
        fail(f"could not read {args.config}: {exc}")

    nims = [n for n in cfg.get("nims", []) if n.get("enabled")]
    if not nims:
        fail("no NIM in config.json has \"enabled\": true", 1)

    problems = []

    ok, detail = check_key_valid(key)
    print(f"[{'PASS' if ok else 'FAIL'}] key            {detail}")
    if not ok:
        problems.append("key is not valid")
        # Every per-NIM check would also fail; stop here so the output is not noise.
        print("\nSkipping per-NIM checks because the key itself did not validate.")
        sys.exit(1)

    for nim in nims:
        repo = repo_from_image(nim["image"])
        tag = str(nim["tag"])
        token, err = registry_token(key, repo)
        if not token:
            print(f"[FAIL] {nim['key']:<14} {repo}:{tag} - {err}")
            problems.append(f"{nim['key']}: {err}")
            continue
        tag_ok, tag_detail, real = check_tag(token, repo, tag)
        if tag_ok:
            print(f"[PASS] {nim['key']:<14} entitled to {repo}, {tag_detail}")
        else:
            hint = ", ".join(real[-8:]) if real else "none visible"
            print(f"[FAIL] {nim['key']:<14} entitled to {repo}, but {tag_detail}")
            print(f"                      available tags: {hint}")
            problems.append(f"{nim['key']}: {tag_detail}")

    print()
    if problems:
        print("PREFLIGHT FAILED - do not create the secret yet:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nAn entitlement failure and a wrong repo name look identical (both 403).\n"
            "Confirm the model at ngc.nvidia.com and that your org has NVAIE\n"
            "entitlement for it."
        )
        sys.exit(1)

    print(f"PREFLIGHT PASSED - key is valid and entitled to all {len(nims)} enabled NIM(s).")
    print("Safe to create the Snowflake secret.")
    sys.exit(0)


if __name__ == "__main__":
    main()
