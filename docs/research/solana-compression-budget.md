# Low-cost Solana deployment and on-chain compression research

**Token-linked v2 follow-up (September 14):** a separate 125,136-byte controller
now mints/burns actual Devnet SPL TEST tokens and transfers mock collateral. Two
test pools, three mint accounts and five token accounts are included in the new
cost model. V2 deployment plus test accounts used **0.65372336 Devnet SOL**, of which
**0.65172336** is locked account funding and **0.002** is upload/deployment/episode fees.

At 300 forecast accounts, retaining the historical v1 reserve and both token pools
requires an estimated **2.39987328 Devnet SOL** in deposits; reserving update buffers
for the active Forecast and token programs raises it to **3.55036120**. A separate
fresh layout with Forecast, one token pool and no historical v1 allocation is
**1.90556388**, or **3.05605180** with the active buffers. No existing accounts were
closed to achieve the hypothetical fresh figure. Fees, metadata, real collateral
and service/data charges are excluded from those layout estimates.
[Token-inclusive inputs and scenarios](evidence/token-devnet-costs-2026-09-14.json).
All figures are Devnet observations/calculations, not new mainnet quotations.

**September 14, 2026 follow-up:** the historical assumptions below are superseded
for combined deployment planning by measured executable allocations: 101,008 bytes
for Forecast and 95,480 bytes for the separate synthetic reserve program. The
[reserve program is deployed on Devnet](https://explorer.solana.com/address/2mrWTuN558NVrs31YdNSc2nyjvNpuexjeLxrTEZhqsP3?cluster=devnet).
The original research and mainnet observations below retain their dated scope.

The updated **Devnet** rent model includes both programs, a Forecast config, one
global synthetic reserve ledger, and 360-byte forecast accounts. It separates
initial deposits from simultaneous upgrade-buffer funding and excludes fees,
RPC/AI/storage, token-mint accounts and economic collateral.

| Forecast accounts | Initial locked Devnet SOL | Including both upgrade buffers |
| ---: | ---: | ---: |
| 3 | 1.01187504 | 2.01171048 |
| 300 | 1.74814992 | 2.74798536 |
| 500 | 2.24395792 | 3.24379336 |

The reserve program plus one ledger contributes 0.48842676 Devnet SOL in locked
rent. These are calculations from the September 14 Devnet RPC rent samples, not
new mainnet quotations. [Inputs, assumptions and calculations](evidence/combined-devnet-rent-2026-09-14.json).
A two-SOL combined target does not cover 300 forecast accounts with both update
buffers reserved.

Research date: 2026-09-09. Basis: the product handoff (private) and completed Milestone 1.
Status: **Milestone 2 design proposal and cost research**. This research did not implement or deploy a contract or access a wallet.

## Conclusion

We recommend **a small verification program, compact active state, and shared commitments for participation, reputation, and completed history**.
The initial target is to operate 300 active forecasts as small individual PDAs while keeping the first deployment and a reserve for an update buffer of the same size within 2 SOL. This is **conditional on a complete verification program building to no more than 64 KiB**; it is not a measured build result.

If 500 active forecasts are required immediately, compare state pages containing 16 records each. With a 48 KiB program, the calculated requirement including an update buffer is approximately 1.94 SOL, but that leaves little reserve and adds page contention and account reuse requirements. Dispute and authorization checks must remain intact when reducing costs. Measure the individual PDA implementation first, then adopt pages if the measured need justifies them.

As individual user records grow into the thousands or tens of thousands, evaluate **Light Compressed PDA v2** separately. Measure initial program size, RPC/prover dependencies, and write fees before introducing it for infrequently updated user reputation and history. Putting all state into a compressed tree is not the initial default.

## 1. Three distinct ways to save

| Cost to reduce | Method | Remaining costs and constraints |
| --- | --- | --- |
| Program code storage | Small native Rust/Pinocchio program containing only required features | ProgramData deposit determined by the final `.so` size |
| Active account data | Fixed binary fields, 32-byte digests, state enum, optional page grouping | Account data and fixed overhead per account, page write locks |
| Large volumes of records | Merkle/append-only commitments or a verified compressed-account protocol | Proofs, indexers, data availability, additional transaction fees |

A Solana program's executable binary is stored in separate on-chain accounts. Compressing state therefore does not eliminate the deployment cost of the Forecast program itself. Initial deployment requires funding the Program and ProgramData accounts and paying transaction fees. Loader-v3 reuses buffer funds to create ProgramData, so **the buffer deposit is not counted twice for the initial deployment**. Later updates require a new funded buffer while the existing ProgramData remains funded.
[Deployment guide](https://solana.com/docs/programs/deploying),
[Anza loader implementation](https://github.com/anza-xyz/agave/blob/master/programs/bpf_loader/src/lib.rs),
[Anza CLI implementation](https://github.com/anza-xyz/agave/blob/master/cli/src/program.rs).

## 2. Comparing approaches

| Approach | Application in this project | Benefits | Cost and verification tradeoffs | Assessment |
| --- | --- | --- | --- | --- |
| Small individual PDAs | Active markets and directly submitted disputes | Simple on-chain guards, separate locks per market | Deposits scale with market count | Initial default |
| 16 fixed records per page | For example, 500 active markets | Saves account overhead | Writes on the same page contend; empty slots are paid for; reuse rules required | Select after measurement |
| SPL Account Compression | Markets/records stored as arbitrary leaves | Can integrate with a custom domain program | Upfront tree/changelog/canopy funding, indexer/proof API | Compare at sufficient leaf count |
| Light Compressed PDA v2 | Large volumes of infrequently updated user/reputation state | Shared trees reduce individual PDA rent requirements | Additional state fees, validity proofs, Photon and operational dependencies | Candidate for scaling |
| Server replaces a single root | Server computes all state | Smallest storage footprint | Does not itself prove correct transitions or complete dispute admission | Unsuitable as the default design |

SPL compression supports arbitrary data commitments; it is not limited to NFTs. However, it requires an external indexer that tracks tree changes and supplies current proofs. The tree write authority must be **a PDA controlled by the Forecast program**, rather than an operator wallet. Bubblegum NFT minting is outside this product.
[Official SPL crate documentation](https://docs.rs/spl-account-compression/latest/spl_account_compression/),
[SPL account-compression implementation](https://github.com/solana-labs/solana-program-library/tree/master/account-compression).

With Light, the custom Forecast program calls Light System to create and update compressed accounts. The official address directory lists mainnet programs and shared v2 trees. This research targets that route and does not assume the conditions of newer sponsored/hot-cold products that also appear in search results. Audits of an existing protocol do not replace verification of the new Forecast program.
[Compressed PDA overview](https://www.zkcompression.com/compressed-pdas/overview),
[Deployed addresses and shared trees](https://www.zkcompression.com/resources/addresses-and-urls),
[Security resources](https://www.zkcompression.com/resources/security).

## 3. Recommended on-chain representation

### Active forecast record: proposed 360 bytes

The following is a design size for explicit fixed binary encoding. It does not copy an arbitrary Rust struct memory layout. The account discriminator is included in the total below.

| Field group | Bytes |
| --- | ---: |
| type discriminator | 8 |
| format/state/paused-from/proposed-outcome/final-outcome/bump | 6 |
| validated flags/reserved | 2 |
| forecast ID commitment, creator pubkey | 64 |
| spec, resolution, dispute-history, audit-history, forecast-checkpoint, policy commitments | 192 |
| revision (`u64`) | 8 |
| challenge epoch, pending/material/accepted disputes (`u32` × 4) | 16 |
| created/published/open/close/updated/challenge-deadline/pause-start/finalized timestamps (`u64` × 8) | 64 |
| **Total** | **360** |

Timestamps use UTC epoch **milliseconds**, as in the existing domain, and retain the existing `2^53−1` bound. Each 64-character hash string is decoded directly into its existing 32-byte digest. **Switching to binary representation does not redefine canonical JSON v1 commitments.** Leaf/node/ID hashes serving different purposes use separate version/type/domain separation.

Rules, sources, original evidence, AI reports, and comments are stored by content address, with the current proposal hash linked to those exact records. Specify the state enum, 0 sentinel, reserved bits, lengths, and endianness, and reject invalid account representations. Recover challenge start history from the transition that created that epoch and its audit record. Validate direct dispute submissions against the current CHALLENGE/DISPUTED state, epoch, and chain time. At finalization, check pending/material counters and the deadline on-chain.

### Dispute receipt: proposed 160 bytes

header 16 + market identity 32 + submitter 32 + dispute hash 32 + review hash 32 +
accepted-at 8 + challenge epoch 4 + reserved 4 = **160**.

For individual PDAs, bind market identity uniquely to the corresponding market. For pages, **the page address alone is insufficient**. Use a canonical market identity digest binding namespace + serial or page/slot/generation, so receipts from different slots cannot be confused. Use that same identity consistently in proposals, reviews, reputation, and archives.

Users must be able to submit directly before the deadline. A sponsor may cover fees in the normal user flow, but inclusion in a server batch must not determine the right to submit a dispute. Atomically create a receipt and increment pending; atomically record exactly one verified review and decrement pending. A review from an older proposal epoch must not apply to a new epoch. Rejecting invalid evidence also requires independent review and an explicit disposition.

The proposed 360/160-byte sizes may increase after reviewing actual instruction account lists, policy storage, and additional replay protection. Keeping only counters while removing supporting receipts is not acceptable.

### Participation, reputation, and completed history

- Preserve signed participation records and anchor them through small batch commitments instead of creating a separate rent-funded account for every participation. **Only records included on-chain before the deadline are shown as confirmed participation.** Design a direct commitment submission path for delayed batches. Retrospective backdating is prohibited.
- Bind each reputation leaf to the user owner, formula version, snapshot revision, and the range of finalized events included. A root alone does not prove that a score was calculated correctly; publish and preserve the inputs needed to recompute it, and prohibit owner changes.
- Consider reclaiming an active account only after its final state and all commitments have entered permanent archive history. Replacing the latest root must not erase prior roots; retain append-only history or a verifiable cumulative commitment.
- A root is a **commitment to data existence and integrity**. It does not provide availability of original text, evidence bytes, leaves, or proofs. Independently recoverable storage and an indexer replay procedure are required.

This batch/archive structure is an M2 proposal for making the Solana representation smaller while preserving complete records externally. It does not discard existing M1 events or receipts. The handoff itself remains unchanged.

## 4. Mainnet observations and conditional budget

The [original RPC observations](evidence/solana-rent-2026-09-09.json) were collected on 2026-09-09 at 12:22 UTC, at confirmed slots 445609765–445609783. The later observation time for the 100-byte entry is recorded separately. These calculations use the returned `getMinimumBalanceForRentExemption` values directly, without reusing a fixed rent formula from older documentation.
[Official RPC definition](https://solana.com/docs/rpc/http/getminimumbalanceforrentexemption).

| Account/allocation | Observed deposit in SOL |
| --- | ---: |
| One 360-byte market PDA | 0.003090504 |
| One 160-byte dispute receipt | 0.001823904 |
| Page with 16 markets + 64-byte header, 5,824 bytes | 0.037694016 |
| 64 KiB program + ProgramData metadata | 0.416135097 |
| SPL depth 14 / buffer 64 / canopy 0, 31,800 bytes | 0.202200024 |
| Same SPL tree + canopy 10, 97,272 bytes | 0.616834200 |

The baseline scenarios include the Program account, a 256-byte config, 4 commitment accounts of 128 bytes each, concurrent dispute receipts equal to 5% of active markets, and **a planned transaction reserve of 0.05 SOL**. The 0.05 reserve is not an observed network fee quote or a lifetime operating budget. No anticipated deposit recovery has been deducted.

| Assumed program size | Active markets/layout | Initial funding + planned reserve | Including a buffer for an update of the same size |
| --- | --- | ---: | ---: |
| 64 KiB | 100 / individual PDAs | **0.794 SOL** | **1.210 SOL** |
| 64 KiB | 300 / individual PDAs | **1.431 SOL** | **1.847 SOL** |
| 64 KiB | 500 / individual PDAs | **2.067 SOL** | **2.483 SOL** |
| 64 KiB | 500 / groups of 16 | **1.728 SOL** | **2.144 SOL** |
| 48 KiB | 500 / groups of 16 | **1.624 SOL** | **1.937 SOL** |
| 96 KiB | 300 / individual PDAs | **1.638 SOL** | **2.262 SOL** |

All figures are **calculations based on assumed layouts and binary sizes**. There is currently no Forecast `.so`. Grouping 500 markets requires paying for all 32 pages/512 slots, and writes within a page contend. The update reserve conservatively adds the corresponding ProgramData rent. Larger updates, failed retries, higher priority fees, or more disputes may require additional funding.

**Dispute sensitivity:** The 5% figure is a planning assumption about concurrent receipt count, not the same statistic as the disputed-market rate in the handoff. If 300 markets have 256 concurrent receipts instead of 15, the 64 KiB individual PDA scenario requires approximately **2.286 SOL**, including the update buffer. This is still not the worst case of 256 receipts per market. Finalization must not be forced through when insufficient sponsor funding has prevented a legitimate challenge from being recorded.

### Light's effect as user account counts grow

At the observed rate, one regular 100-byte PDA requires 0.001443924 SOL, so 10,000 require approximately **14.44 SOL in deposits**. Under the Light v2 documentation's conditions of one signature, one instruction, one tree, and one leaf, creating an addressed compressed account costs `5,000 + 5,000 + approximately 300 + 10,000 = approximately 20,300 lamports`; an update costs approximately 10,300 lamports. Creating 10,000 accounts costs approximately **0.203 SOL in transaction/state fees**. Creating 1,000 accounts and updating each 11 times costs approximately **0.1336 SOL**.
[Light cost and constraint table](https://www.zkcompression.com/learn/considerations).

These funding requirements also differ in nature. Regular PDA funding is a deposit tied up while the account is maintained; compressed-operation fees are consumed. Both exclude deployment of the custom program and RPC/prover charges. Multiple trees, signers, instructions, more leaves, and failed transactions increase costs. Compression should not automatically be applied to a global probability account that is updated most frequently.

An SPL tree does not cost only the 32 bytes of its root. The official SDK serialization size is:

```text
56 + 24 + (buffer + 1) * (32 * depth + 40)
   + 32 * (2^(canopy + 1) - 2)
```

A depth 14/buffer 64 tree holds 16,384 leaves, but they do not all need to be created initially. A canopy reduces transmitted proof size in exchange for more prepaid storage. Verify supported depth/buffer combinations and serialized instruction transaction sizes against the SDK and chain before deployment.
[SDK account size](https://github.com/solana-labs/solana-program-library/blob/master/account-compression/sdk/src/accounts/ConcurrentMerkleTreeAccount.ts),
[Tree structure](https://github.com/solana-labs/solana-program-library/blob/master/account-compression/sdk/src/types/ConcurrentMerkleTree.ts),
[Path structure](https://github.com/solana-labs/solana-program-library/blob/master/account-compression/sdk/src/types/Path.ts).

## 5. Reducing program code size

Pinocchio is a native Rust library designed for small binaries and low compute costs. It permits fixed encoding and exclusion of unnecessary allocations/features, but the implementation remains responsible for signer, owner, PDA derivation, duplicate account, length, and overflow validation. A “hello world 5–15 KiB” example is not a cost basis for this project.
[Anza Pinocchio](https://github.com/anza-xyz/pinocchio),
[Solana Pinocchio template](https://solana.com/developers/templates/pinocchio-counter).

In M2, compare native/Pinocchio with a minimal Anchor configuration if needed, using identical functionality and tests. Measure actual ELF size and CU for `opt-level="s"/"z"`, LTO, codegen-units, and symbol stripping individually. Do not assume any particular flag always reduces size. Retain explicit checked arithmetic and validation while reducing unnecessary formatting/logging.
[Cargo profile documentation](https://doc.rust-lang.org/cargo/reference/profiles.html).

It is unnecessary to port the entire existing JSON schema and commitment computation onto the chain. However, the program must verify **authenticated attestations and version/hash binding** for off-chain checks. Reuse deployed crypto/syscalls and compression verifiers without assuming those programs also enforce Forecast domain rules. Library adoption and version pinning belong to M2 implementation; this research added no dependencies.

## 6. Verification that must survive budget reductions

1. Prohibit changes to published specs/sources/rules; bind exact evidence/provenance hashes.
2. Enforce signer authority by role, approved independent reviewers and policy versions, and upgrade authority responsibilities.
3. Enforce CAS revisions and stable command identities; prevent duplicate disputes, reviews, and finalizations.
4. Finalize using the current epoch/deadline and pending/material counters.
5. Require a new challenge after changed adjudication; prohibit finalization from PAUSED; extend deadlines on recovery.
6. Support direct dispute submission even when omitted from an operator batch, with recovery paths for indexer/proof failures.
7. If a previous leaf has been consumed or a proof is stale, **re-evaluate the command** against current state. Do not blindly resubmit an old command with only a newer proof attached.
8. Prevent recreation of the same market address/ID after archive from awarding reputation twice.

Preventing recreation after account closure remains an implementation requirement to specify. A permanent tombstone per market would reintroduce substantial costs. An alternative is a persistent, monotonically increasing serial in config, a namespace/seed that is never reused, and a verified archive registry. Page layouts that reuse slots need separate review of generation and canonical identity. New market initialization must accept exactly `next_serial` and increment it atomically. The allocator must not permit reset/rollback or reinitialization with an old serial. This mechanism alone does not prevent an existing arbitrary domain string ID from being attached to a different serial, so M2 must separately define canonical ID mapping and uniqueness of existing IDs. Do not silently recompute a published M1 commitment under a new ID. Closing or recycling a page first requires finalization, archival, and identity protection for every slot. Do not count reclaimed rent as budget savings before these conditions are satisfied.

The current domain limit of 256 disputes could also obstruct submissions if an attacker fills it first. Define fair admission, additional capacity, or automatic pause/escalation when capacity is reached. Do not finalize solely because “pending=0” if legitimate disputes could not be admitted.

Light/SPL proofs do not establish the truth of AI judgments or prove that data can actually be downloaded. Proof/indexer/queue operations, additional CU, and transaction size are also within the design scope.
[Light transaction lifecycle](https://www.zkcompression.com/learn/transaction-lifecycle),
[Light protocol paper](https://github.com/Lightprotocol/light-protocol/blob/main/light-paper.md),
[SPL proof/indexer documentation](https://docs.rs/spl-account-compression/latest/spl_account_compression/).

## 7. Verification sequence and completion criteria before deployment

1. Build a **verification program with equivalent functionality**: proposed 360/160-byte codecs, role authorization, and every M1 lifecycle condition. Check canonical commitments and binary golden vectors in CI.
2. Measure the full SBF build, account sizes, and serialized size/CU of the worst-case instruction. If the build exceeds 64 KiB, recalculate the budget, active scale, or account structure while retaining validation.
3. Verify create → participate → close → propose → directly dispute → review → finalize → archive on Devnet. Include multiple users, stale proofs, concurrent dispute/finalize operations, pause, duplicate retries, receipt removal/recreation, and archive ID replay.
4. Test challenge epochs for new proposals, explicit rejection of invalid evidence, counter under/overflow, max capacity, signer forgery, account aliasing, missing proofs, and data availability failures.
5. Immediately before deployment, query rent again for the actual `.so` and explicit allocation lengths. Evaluate the budget ceiling including initial state, dispute capacity, at least one update buffer, and expected transaction retries.

This research treats 2 SOL as **an on-chain funding design target**. It excludes servers, AI APIs, evidence storage, RPC/prover/indexer services, audits, and indefinite operating costs. Actual deployment feasibility can only be confirmed after building and verifying the complete program.

## 8. Reproducible artifacts

- [Original rent responses and timestamps](evidence/solana-rent-2026-09-09.json)
- [Integer lamport calculations by scenario](evidence/compression-cost-scenarios.json)
- Calculator: `python3 scripts/estimate_solana_storage.py` — uses neither a network nor a wallet.
- Calculation regression checks: `python3 -m unittest tests.test_solana_costs`
- Full regression checks: `python3 scripts/check.py --tools`

The calculator fails for account sizes without observations instead of substituting an arbitrary formula. Tests cover SPL size, rounding page counts upward, Light signature/tree/leaf/address fees, separation of initial deployment and update funding, and the conditional 2 SOL assessment. JSON separates price observations from design assumptions.

Verification results at the time of this research: 84 tests, including the existing domain checks, plus schema drift, Ruff, and strict domain mypy passed. The calculator also passed strict mypy and 8 arithmetic/layout checks, and the saved scenario JSON matches its calculations. Findings from a separate design review about market identity within pages and budget overruns during dispute surges are incorporated. This research did not measure actual SBF size, CU, or Devnet program behavior.
