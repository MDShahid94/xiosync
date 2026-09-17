"""XIORUN — Browser Runtime Provisioner.

Orchestrates remote patchright/Chromium browser instances running on Colab
workers (100.x.x.y via Tailscale). Handles the full lifecycle:

  launch   → resolve Colab node → command xiorun-agent → CDP attach
           → fingerprint injection → Chrome profile restore (R2 tar.gz)
           → cookie fallback (vault) → exit-node guard → register with XIOVIEW

  teardown → save profile to R2 → close CDP → suspend session

Anti-degradation guarantees:
  - Exit-node loss = immediate hard kill. No fallback to datacenter IP.
  - Cookie save: TTL-aware merge, mid-auth guard, SID-TTL check.
  - Emergency save on browser disconnect: non-Google cookies only.
"""
