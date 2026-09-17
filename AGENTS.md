# Forecast Network repository conventions

The product and architecture handoff is maintained in the private companion repository;
`docs/blueprint.md` and `docs/roadmap.md` are its public expression. Implementation decisions are documented in `docs/architecture/`.

- Keep the domain independent of application frameworks, databases, clocks and
  network/provider clients. External effects belong in later adapters.
- Preserve the non-monetary/non-transferable constraints and immutable published
  specifications. Never bypass evidence, dispute or finalization gates.
- Domain records are frozen and recursively immutable. An annotation/field metadata
  change must update the generated versioned contracts through the generator;
  never maintain parallel hand-edited schema definitions.
- Test semantic guards on both commands and decoded snapshots. Error paths must
  leave the original aggregate unchanged. Keep validation active under Python `-O`.
- Run `python3 scripts/check.py`; run `python3 scripts/check.py --tools` when the
  existing Ruff/mypy tools are available. Do not add runtime dependencies without
  an explicit request.
- Commit each completed work unit before starting the next, especially during a
  long autonomous run. Leave the repository committable at every stopping point:
  never let days of work accumulate in the working tree, and never leave code that
  is serving production untracked. Migrations, generated schemas and the tests for
  a change belong in the same commit as that change. A commit is a checkpoint, not
  a reward for finishing.
- Store temporary files, build copies and caches under repository `tmp/`. Set
  `TMPDIR` to its absolute path and use `PYTHONDONTWRITEBYTECODE=1` during ad hoc
  Python work. Set tool-specific cache paths there as needed.
- In the Cloudflare Python Worker, never run concurrent Python tasks that each await
  a binding promise (`asyncio.gather`, `create_task` fan-out over D1/fetch). Pyodide's
  promising-task runtime fails the next request on the isolate with an empty 500. Await
  one JS promise at a time and use `D1.batch()` to fan out reads in one round trip.
- Do not claim durable CAS, authenticated attestations, on-chain verification or
  delivered notifications until the corresponding adapters are implemented and tested.
