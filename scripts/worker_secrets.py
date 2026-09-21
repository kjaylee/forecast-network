#!/usr/bin/env python3
"""The runtime secrets each Worker is deployed with, as data both deploy scripts share.

`deploy_web.py` and `deploy_edge.py` each held their own list, and the edge's said "session
hashing and provider presence only" after the crate had started reading the operator token,
the relayer seed and the registry credentials. A list that lives in a deploy script is read
by nobody until the deploy; a list that lives here is read by `config_parity.py` on every
`check.py` run, against what the crate actually reads.

The names are the contract. The values come from the Keychain at deploy time and never
leave `secret_payload`'s caller.
"""

from __future__ import annotations

import base64
from typing import Any

# Read by both Workers unconditionally.
ALWAYS = ("SESSION_SECRET", "ADMIN_TOKEN", "AI_PROXY_TOKEN", "SCHEDULER_TOKEN", "SOLANA_DEVNET_RPC_KEYED")
# Read only when the registry is enabled; the relayer seed is checked against the configured
# address before it is sent, because a seed for another identity signs transactions the chain
# rejects — after the spend was authorized.
REGISTRY = ("SOLANA_RELAYER_SEED", "SOLANA_RPC_PROXY_TOKEN")
# Optional keyed mainnet RPC (Helius/QuickNode/...): public endpoints throttle Cloudflare.
OPTIONAL = ("SOLANA_MAINNET_RPC_KEYED",)

# The Python Worker and the edge now serve the same surface, so they are deployed with the
# same secrets. `GEMINI_API_KEY` is deliberately absent from both: it lives in the relay Worker
# alone, and each Worker honours a direct key only when no relay is configured.
PYTHON_WORKER_SECRETS = ALWAYS + REGISTRY + OPTIONAL
EDGE_WORKER_SECRETS = ALWAYS + REGISTRY + OPTIONAL


def secret_payload(config: dict[str, Any]) -> dict[str, str]:
    """The `wrangler secret bulk` body for a Worker whose `wrangler.jsonc` is `config`."""
    from cloudflare_keychain import KEYCHAIN_SERVICES, secret
    from solana_keychain import Keychain, base58, public_bytes

    payload = {name: secret(name) for name in ALWAYS}
    variables = config.get("vars", {})
    if variables.get("SOLANA_REGISTRY_ENABLED") == "true":
        seed = Keychain().read("relayer")
        if seed is None or base58(public_bytes(seed)) != variables.get("SOLANA_RELAYER"):
            raise RuntimeError("Devnet relayer Keychain identity does not match deployment")
        payload["SOLANA_RELAYER_SEED"] = base64.b64encode(seed).decode("ascii")
        if variables.get("SOLANA_RPC_PROXY_URL"):
            proxy_seed = Keychain(services={"gateway": "forecast-network-devnet-rpc-gateway-v1"},
                                  account=b"forecast-rpc").read("gateway")
            if proxy_seed is None:
                raise RuntimeError("Scoped Devnet RPC gateway credential must be provisioned first")
            payload["SOLANA_RPC_PROXY_TOKEN"] = proxy_seed.hex()
    if "SOLANA_MAINNET_RPC_KEYED" in KEYCHAIN_SERVICES:
        payload["SOLANA_MAINNET_RPC_KEYED"] = secret("SOLANA_MAINNET_RPC_KEYED")
    return payload
