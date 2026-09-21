"""The configuration parity gate, proved by the drifts it exists to catch.

Each test removes or renames one thing the gate claims to check and asserts the gate fails on
it — the discipline the SQL and route gates were put through. A check that passes on the tree
proves nothing until it has been watched failing on the tree's mutation.
"""
from __future__ import annotations

import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import config_parity  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class ExtractionTests(unittest.TestCase):
    def test_every_edge_read_shape_is_seen(self):
        source = '''
            let a = var(env, "ALPHA");
            let b = var(context.env, "BETA");
            let c = env.secret("GAMMA").ok();
            let d = crate::admin::flag(env, "DELTA");
            let e = crate::admin::switch(context.env, "EPSILON", true);
            let f = env.d1("DB")?;
            let g = env.service("LEGACY")?;
            let h = env.assets("ASSETS")?;
            let i = env.get_binding::<worker::Ai>("AI").ok();
            let var = |name: &str| env.var(name).map(|v| v.to_string()).unwrap_or_default();
            let j = var("ZETA");
        '''
        self.assertEqual(config_parity.edge_names(source),
                         {"ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON", "DB", "LEGACY", "ASSETS", "AI", "ZETA"})

    def test_a_read_inside_a_test_module_is_not_a_production_read(self):
        source = 'let a = var(env, "LIVE");\n#[cfg(test)]\nmod tests {\n    let b = var(env, "TEST_ONLY");\n}\n'
        self.assertEqual(config_parity.edge_names(source), {"LIVE"})

    def test_a_test_module_declaration_at_the_top_does_not_hide_the_file(self):
        # `lib.rs` declares `#[cfg(test)] mod golden;` before the entry that reads every binding.
        source = '#[cfg(test)]\nmod golden;\n\nfn main(env: Env) {\n    env.assets("ASSETS");\n}\n'
        self.assertEqual(config_parity.edge_names(source), {"ASSETS"})

    def test_every_reference_read_shape_is_seen(self):
        source = '''
            url = str(getattr(self.env, "SOLANA_RPC_URL", ""))
            token = getattr(bindings, "ADMIN_TOKEN", None)
            other = getattr(env, "OTHER")
            model = str(self.env.GEMINI_MODEL)
            response = await bindings.SCHEDULED_JOBS.fetch(url)
        '''
        self.assertEqual(config_parity.python_names(source),
                         {"SOLANA_RPC_URL", "ADMIN_TOKEN", "OTHER", "GEMINI_MODEL", "SCHEDULED_JOBS"})


class GateTests(unittest.TestCase):
    def setUp(self):
        self.edge = config_parity.edge_reads()
        self.python = config_parity.python_reads()
        self.edge_config = json.loads((ROOT / "apps/web-rs/wrangler.jsonc").read_text())
        self.python_config = json.loads((ROOT / "apps/web/wrangler.jsonc").read_text())

    def check(self) -> list[str]:
        return config_parity.problems(self.edge, self.python, self.edge_config, self.python_config)

    def test_the_repository_is_actually_in_parity(self):
        self.assertEqual(self.check(), [])

    def test_a_var_the_edge_reads_but_does_not_declare_fails(self):
        # The drift that was live for a week: the crate read a name its wrangler.jsonc lacked.
        del self.edge_config["vars"]["SOLANA_RELAYER"]
        self.assertTrue(any(problem.startswith("SOLANA_RELAYER: read by the edge") for problem in self.check()))

    def test_a_name_read_under_a_different_spelling_fails(self):
        # `SOLANA_DEVNET_RPC` for `SOLANA_RPC_URL`: a name neither Worker is deployed with.
        self.edge["SOLANA_DEVNET_RPC"] = self.edge.pop("SOLANA_RPC_URL")
        problems = self.check()
        self.assertTrue(any(problem.startswith("SOLANA_DEVNET_RPC: read only by the edge") for problem in problems))
        self.assertTrue(any(problem.startswith("SOLANA_RPC_URL: read only by the reference") for problem in problems))

    def test_a_secret_the_deploy_script_stops_pushing_fails(self):
        with unittest.mock.patch.object(config_parity, "EDGE_WORKER_SECRETS",
                                        tuple(n for n in config_parity.EDGE_WORKER_SECRETS if n != "ADMIN_TOKEN")):
            self.assertTrue(any(problem.startswith("ADMIN_TOKEN: read by the edge") for problem in self.check()))

    def test_a_missing_binding_fails(self):
        del self.edge_config["ai"]
        self.assertTrue(any(problem.startswith("AI: read by the edge") for problem in self.check()))

    def test_a_var_declared_with_two_values_fails(self):
        self.edge_config["vars"]["SOLANA_RPC_URL"] = "https://api.mainnet-beta.solana.com"
        self.assertIn("SOLANA_RPC_URL: declared with different values in the two wrangler.jsonc files", self.check())

    def test_a_reviewed_difference_that_stopped_being_one_fails(self):
        self.python["LEGACY"] = {"apps/web/src/entry.py"}
        self.assertIn("LEGACY: listed as a reviewed difference but both Workers read it", self.check())

if __name__ == "__main__":
    unittest.main()
