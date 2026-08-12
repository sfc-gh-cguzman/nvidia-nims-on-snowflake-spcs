#!/usr/bin/env python3
"""Call an SPCS ingress endpoint using a session token from an existing connection.

No PAT required. The Python Connector authenticates however connections.toml is
already configured (local OAuth, SSO, key pair - it does not matter), then issues a
session token that the SPCS ingress accepts as `Snowflake Token="..."`.

This is Option 3 from Snowflake's "Access the public endpoint programmatically"
tutorial. It relies on `conn._rest._token_request('ISSUE')`, a PRIVATE connector API,
so it can break on a connector upgrade. Prefer a PAT for anything durable.

Usage:
  spcs_call.py --connection <name> --service <db.schema.service> --token-only
  spcs_call.py --connection <name> --service <db.schema.service> \
               --path /generate --json '{"num_molecules": 5}'
"""
import argparse
import json
import sys

import requests
import snowflake.connector


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", required=True)
    ap.add_argument("--service", required=True, help="db.schema.service")
    ap.add_argument("--path", default="/")
    ap.add_argument("--json", dest="body", default=None, help="JSON request body")
    ap.add_argument("--token-only", action="store_true",
                    help="acquire the token and report on it, make no request")
    a = ap.parse_args()

    with snowflake.connector.connect(
        connection_name=a.connection,
        session_parameters={"PYTHON_CONNECTOR_QUERY_RESULT_FORMAT": "json"},
    ) as conn:
        try:
            token = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED to issue session token: {type(exc).__name__}: {exc}")
            return 1
        # Never print the token itself.
        print(f"session token acquired: length={len(token)}, non-empty={bool(token)}")
        if a.token_only:
            return 0 if token else 1

        host = None
        for row in conn.cursor().execute(f"SHOW ENDPOINTS IN SERVICE {a.service}"):
            # columns: name, port, port_range, protocol, is_public, ingress_url
            if str(row[4]).lower() == "true":
                host = row[5]
                break
        if not host:
            print("no public endpoint found on that service")
            return 1
        url = f"https://{host}{a.path}"
        print(f"POST {url}")

        headers = {"Authorization": f'Snowflake Token="{token}"'}
        if a.body:
            headers["Content-Type"] = "application/json"
            r = requests.post(url, headers=headers, data=a.body, timeout=180)
        else:
            r = requests.get(url, headers=headers, timeout=180)

        print(f"HTTP {r.status_code}")
        try:
            print(json.dumps(r.json(), indent=2)[:1500])
        except Exception:  # noqa: BLE001
            print(r.text[:1000])
        return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
