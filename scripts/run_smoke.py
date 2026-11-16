import json
import os
import time

import httpx

from bridge import config
from bridge.contracts import TERMINAL

GOAL = "Escalate unresolved high-priority Team A tickets older than 48 hours. Check each linked CRM customer."


def main():
    base = os.getenv("PUBLIC_APP_URL", config.BROWSER_URL).rstrip("/")
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=10, trust_env=False) as client:
        client.get("/health/ready").raise_for_status()
        response = client.post(
            "/api/v1/auth/login", json={"email": "operator@demo.test", "password": config.DEV_PASSWORD}
        )
        response.raise_for_status()
        response = client.post("/api/v1/runs", json={"goal": GOAL, "mode": "hybrid"})
        response.raise_for_status()
        identifier = response.json()["id"]
        for _ in range(160):
            run = client.get(f"/api/v1/runs/{identifier}").raise_for_status().json()
            if run["status"] in TERMINAL:
                report = client.get(f"/api/v1/runs/{identifier}/report").raise_for_status().json()
                print(json.dumps(report, indent=2))
                if run["status"] == "COMPLETED" and report["outcomes"]:
                    return
                raise SystemExit("The smoke run did not complete")
            time.sleep(0.5)
        raise SystemExit(f"Timed out waiting for {identifier}; inspect worker logs")


if __name__ == "__main__":
    main()
