"""Credential namespaces must not redirect the existing Devnet signing roles."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

try:
    from scripts import solana_keychain
except ImportError as error:  # pragma: no cover
    # The dependency-free CI job exercises the domain without installing anything,
    # and the signer reads its keys through cryptography.
    raise unittest.SkipTest(f"cryptography is required: {error}") from error


class KeychainNamespaceTests(unittest.TestCase):
    def test_default_signing_roles_retain_the_original_service_and_account(self):
        security = MagicMock()
        security.SecKeychainFindGenericPassword.return_value = -25300
        with patch.object(solana_keychain.ctypes, "CDLL", return_value=security):
            keychain = solana_keychain.Keychain()
            for role, service in solana_keychain.SERVICES.items():
                self.assertIsNone(keychain.read(role))
                args = security.SecKeychainFindGenericPassword.call_args.args
                self.assertEqual(args[2], service.encode())
                self.assertEqual(args[4], solana_keychain.ACCOUNT)

    def test_gateway_namespace_is_isolated_and_copied(self):
        security = MagicMock()
        security.SecKeychainFindGenericPassword.return_value = -25300
        services = {"gateway": "forecast-network-devnet-rpc-gateway-v1"}
        original = dict(solana_keychain.SERVICES)
        with patch.object(solana_keychain.ctypes, "CDLL", return_value=security):
            keychain = solana_keychain.Keychain(services=services, account=b"forecast-rpc")
            services["gateway"] = "replacement-must-not-affect-instance"
            self.assertIsNone(keychain.read("gateway"))
            args = security.SecKeychainFindGenericPassword.call_args.args
            self.assertEqual(args[2], b"forecast-network-devnet-rpc-gateway-v1")
            self.assertEqual(args[4], b"forecast-rpc")
            with self.assertRaises(KeyError):
                keychain.read("relayer")
        self.assertEqual(solana_keychain.SERVICES, original)
