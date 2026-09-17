#!/usr/bin/env python3
"""Launch the owned Devnet gateway using a separate Keychain credential.

The random gateway token is not a Solana signing key. No secret appears in the
launchd plist, command arguments or logs. Existing signing namespaces are unused.
"""
from __future__ import annotations

import os

from devnet_rpc_proxy import TOKEN_ENV, main
from solana_keychain import Keychain

if __name__ == "__main__":
    seed = Keychain(services={"gateway": "forecast-network-devnet-rpc-gateway-v1"},
                    account=b"forecast-rpc").read("gateway")
    if seed is None:
        raise RuntimeError("Provision the scoped Devnet gateway credential first")
    os.environ[TOKEN_ENV] = seed.hex()
    del seed
    main()
