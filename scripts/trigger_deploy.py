#!/usr/bin/env python3
"""Trigger a Render deploy using a webhook URL."""

import json
import os
import sys
from urllib import request, error


def main():
    url = os.getenv("RENDER_DEPLOY_WEBHOOK_URL")
    if not url:
        print("Missing RENDER_DEPLOY_WEBHOOK_URL environment variable.")
        print("Example: set RENDER_DEPLOY_WEBHOOK_URL=https://api.render.com/deploy/srv-...")
        return 1

    payload = json.dumps({"trigger": "manual"}).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "render-trigger/1.0",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=30) as response:
            print(f"Render deploy webhook triggered successfully: HTTP {response.status}")
            print(response.read().decode("utf-8", errors="replace"))
            return 0
    except error.HTTPError as exc:
        print(f"Render deploy webhook failed: HTTP {exc.code}")
        print(exc.read().decode("utf-8", errors="replace"))
        return 1
    except Exception as exc:
        print(f"Render deploy webhook error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
