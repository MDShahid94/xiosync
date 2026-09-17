"""xiosync_client.py — lightweight XIOSYNC API client for Colab workers.

Pure stdlib (no third-party deps). Handles JWT login + automatic token
refresh so callers never worry about the 15-minute expiry.

New in v2:
    - self_enroll()       — autonomous worker registration via org secret
    - heartbeat()         — update last_seen_at + reported_caps
    - poll_dag_run()      — claim a PENDING xioflow_dag run
    - complete_run()      — report run completion back to XIOSYNC
    - fire_trigger()      — manually fire a named trigger
    - list_runs()         — inspect run history

Usage (boot.py):
    from xiosync_client import XIOSyncClient

    client = XIOSyncClient(
        base_url="http://100.86.149.127:8000",
        org_id="00000000-0000-7000-8000-000000000000",
        email="admin@xiogrid.dev",
        password="Xiogrid2026!Admin",
        worker_org_secret="xiogrid-worker-org-secret-2026-karma",
        worker_ts_ip="100.120.53.61",
    )

    # Self-enroll on boot (autonomous — no pre-existing JWT needed via org secret)
    enrollment = client.self_enroll(runtime_type="colab")
    enrollment_id = enrollment["enrollment_id"]

    # Heartbeat in keep-alive loop
    client.heartbeat(enrollment_id)

    # Acquire a PPPoE slot
    slot = client.acquire_slot(session_id="my-session", worker_ts_ip="100.x.x.x")

    # Poll for a pending DAG run and execute it locally
    run = client.poll_dag_run()
    if run:
        # execute locally via DAGExecutor …
        client.complete_run(run["run_id"], run["task_id"], success=True)
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any


class XIOSyncAuthError(Exception):
    pass


class XIOSyncClient:
    """Thread-safe XIOSYNC API client with automatic JWT refresh."""

    def __init__(
        self,
        base_url: str,
        org_id: str,
        email: str,
        password: str,
        worker_org_secret: str = "",
        worker_ts_ip: str = "",
        timeout: int = 90,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.org_id = org_id
        self.email = email
        self.password = password
        self.worker_org_secret = worker_org_secret
        self.worker_ts_ip = worker_ts_ip
        self.timeout = timeout

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires_at: float = 0.0  # epoch seconds

    # ── PPPoE ─────────────────────────────────────────────────────────────────

    def acquire_slot(
        self,
        session_id: str,
        worker_ts_ip: str,
        preferred_host_id: str | None = None,
    ) -> dict[str, Any]:
        """Acquire an idle PPPoE slot (auto-provisions if pool empty)."""
        body: dict[str, Any] = {"session_id": session_id, "worker_ts_ip": worker_ts_ip}
        if preferred_host_id:
            body["preferred_host_id"] = preferred_host_id
        return self._post("/api/v1/pppoe/nodes/acquire", body)

    def release_slot(self, host_id: str, slot: int) -> dict[str, Any]:
        """Return a slot to the idle pool."""
        return self._delete(f"/api/v1/pppoe/nodes/{host_id}/{slot}/assign")

    def list_hosts(self) -> dict[str, Any]:
        return self._get("/api/v1/pppoe/hosts")

    def list_slots(self, host_id: str) -> dict[str, Any]:
        return self._get(f"/api/v1/pppoe/nodes/{host_id}")

    # ── Worker enrollment ─────────────────────────────────────────────────────

    def self_enroll(
        self,
        runtime_type: str = "colab",
        reported_caps: list[str] | None = None,
        software_version: str = "xiosync-client-v2",
    ) -> dict[str, Any]:
        """Enroll this worker autonomously using the shared org secret.

        Does NOT require a pre-existing JWT — authenticates via worker_org_secret.
        Returns dict with enrollment_id, enrollment_state, enrollment_token.
        """
        if not self.worker_org_secret:
            raise ValueError("worker_org_secret is required for self_enroll()")
        return self._post(
            "/api/v1/workers/self-enroll",
            {
                "worker_org_secret": self.worker_org_secret,
                "runtime_type": runtime_type,
                "tailscale_ip": self.worker_ts_ip,
                "reported_caps": reported_caps or ["browser.run", "pppoe.use", "xioflow.execute"],
                "software_version": software_version,
            },
        )

    def heartbeat(
        self,
        enrollment_id: str,
        reported_caps: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update last_seen_at for an enrolled worker.

        Call this from the keep-alive loop every 30s.
        """
        body: dict[str, Any] = {"tailscale_ip": self.worker_ts_ip}
        if reported_caps:
            body["reported_caps"] = reported_caps
        return self._post(f"/api/v1/workers/{enrollment_id}/heartbeat", body)

    # ── Triggers ──────────────────────────────────────────────────────────────

    def list_triggers(
        self,
        trigger_type: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        qs = []
        if trigger_type:
            qs.append(f"trigger_type={trigger_type}")
        if enabled is not None:
            qs.append(f"enabled={'true' if enabled else 'false'}")
        path = "/api/v1/triggers" + (("?" + "&".join(qs)) if qs else "")
        return self._get(path)  # type: ignore[return-value]

    def fire_trigger(
        self,
        trigger_id: str,
        extra_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Manually fire a trigger — returns {run_id, state}."""
        return self._post(
            f"/api/v1/triggers/{trigger_id}/fire",
            {"extra_context": extra_context or {}},
        )

    # ── XIOFLOW Run management ────────────────────────────────────────────────

    def list_runs(
        self,
        state: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        qs = [f"limit={limit}"]
        if state:
            qs.append(f"state={state}")
        resp = self._get(f"/api/v1/xioflow/events/runs?{'&'.join(qs)}")
        return resp.get("runs", resp) if isinstance(resp, dict) else resp  # type: ignore[return-value]

    def poll_dag_run(self) -> dict[str, Any] | None:
        """Atomically claim one PENDING xioflow_dag run for local execution.

        Returns run details dict or None if no pending DAG runs.
        The run is immediately marked RUNNING server-side (FOR UPDATE SKIP LOCKED).

        Returned dict:
            run_id, task_id, context, template_name, root_memory_node_id
        """
        resp = self._request("GET", "/api/v1/xioflow/events/runs/pending-dag")
        # 204 No Content → None
        if not resp:
            return None
        return resp

    def complete_run(
        self,
        run_id: str,
        task_id: str | None = None,
        success: bool = True,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Report DAG run completion to XIOSYNC.

        Call this after local DAGExecutor finishes (success or failure).
        """
        return self._post(
            f"/api/v1/xioflow/events/runs/{run_id}/complete",
            {
                "success": success,
                "task_id": task_id,
                "result": result or {},
                "error": error,
            },
        )

    # ── Misc ──────────────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Check XIOSYNC liveness (no auth required)."""
        return self._request("GET", "/live", auth=False)

    def list_templates(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/xioflow/templates")  # type: ignore[return-value]

    # ── Auth internals ────────────────────────────────────────────────────────

    def _ensure_token(self) -> str:
        now = time.time()
        if self._access_token and self._token_expires_at > now + 30:
            return self._access_token
        if self._refresh_token:
            try:
                self._do_refresh()
                return self._access_token  # type: ignore[return-value]
            except XIOSyncAuthError:
                pass
        self._do_login()
        return self._access_token  # type: ignore[return-value]

    def _do_login(self) -> None:
        data = self._request(
            "POST", "/api/v1/auth/login",
            body={"organization_id": self.org_id, "email": self.email, "password": self.password},
            auth=False,
        )
        self._store_tokens(data)

    def _do_refresh(self) -> None:
        data = self._request(
            "POST", "/api/v1/auth/refresh",
            body={"refresh_token": self._refresh_token},
            auth=False,
        )
        self._store_tokens(data)

    def _store_tokens(self, data: dict[str, Any]) -> None:
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        expires_str = data.get("access_token_expires_at", "")
        try:
            dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            self._token_expires_at = dt.timestamp()
        except (ValueError, AttributeError):
            self._token_expires_at = time.time() + 840

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body=body)

    def _delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", path)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._ensure_token()}"

        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 204:
                    return {}
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                err = json.loads(raw)
            except ValueError:
                err = {"detail": raw}
            if exc.code in (401, 403):
                raise XIOSyncAuthError(
                    f"Auth failed [{exc.code}]: {err.get('detail', raw)}"
                ) from exc
            raise RuntimeError(
                f"XIOSYNC API error [{exc.code}] {method} {path}: {err}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"XIOSYNC unreachable at {url}: {exc.reason}") from exc


# ── Convenience singleton ─────────────────────────────────────────────────────

def make_client_from_env() -> XIOSyncClient:
    """Build a client from environment variables.

    Required:
        XIOSYNC_BASE_URL   — e.g. http://100.86.149.127:8000
        XIOSYNC_ORG_ID     — 00000000-0000-7000-8000-000000000000
        XIOSYNC_EMAIL      — admin@xiogrid.dev
        XIOSYNC_PASSWORD   — Xiogrid2026!Admin

    Optional:
        XIOSYNC_WORKER_SECRET  — for self_enroll()
        XIOSYNC_TS_IP          — this worker's Tailscale IP
    """
    import os
    return XIOSyncClient(
        base_url=os.environ["XIOSYNC_BASE_URL"],
        org_id=os.environ["XIOSYNC_ORG_ID"],
        email=os.environ["XIOSYNC_EMAIL"],
        password=os.environ["XIOSYNC_PASSWORD"],
        worker_org_secret=os.environ.get("XIOSYNC_WORKER_SECRET", ""),
        worker_ts_ip=os.environ.get("XIOSYNC_TS_IP", ""),
    )
