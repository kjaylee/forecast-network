# Off-host liveness: borrowed triggers, a DHT beacon, and torrented backups

The target is stated in [operational-criticality](operational-criticality.md):
production ends up separated from this operator's own system. No Mac, no NAS, no
house supply on the critical path.

Three strands, each investigated on 2026-09-18. Two are proven enough to build
on, one is not. The measurements and the negative results are recorded here
because the negative results are the useful part.

## Strand 1 — borrow someone else's scheduler

Triggers can be borrowed; liveness cannot. What can actually be borrowed:

| Service | Cadence | Verified |
| --- | --- | --- |
| **cron-job.org** | **1 minute**, up to 60/hour | Free, no card. POST and arbitrary headers supported; `User-Agent` and `Connection` are ignored. 30 s request timeout, 64 KB response cap, last 50 runs retained 2 days, **job auto-disables after 25 consecutive failures**. |
| **healthchecks.io** | inverse direction | Free Hobbyist: **20 checks**, 100 log entries per check, email/Slack/webhook/Discord/PagerDuty alerts, grace periods, explicit `/fail` pings, cron-expression or interval schedules. BSD-3-Clause and self-hostable. |
| Cloudflare Worker cron | 5 minutes | Already used, and already off-host. |
| GitHub Actions `schedule` | 5 minutes minimum, best effort | Already used for the daily editorial seed. Disabled after 60 days without repository activity. |

**Contradiction to resolve before relying on cron-job.org:** its own FAQ says
there is no job-count limit, subject to fair use, while two third-party 2026
comparisons claim 7 jobs. The official source is the more likely correct one,
but the number should be confirmed on the account before it is depended on.

**healthchecks.io inverts the problem, which is why it matters more here.** A
poller asks "is it broken?"; a dead-man's switch asks "did it report at all?".
The failure this system actually suffered — a job that silently stopped running
for two days — is invisible to the first question and immediate in the second.
The per-minute operator and the five-minute sweep should ping it on success.

Both are separate failure domains from the operator host, Cloudflare and GitHub,
which is the property being bought.

Unverified and deliberately not recommended: Oracle Cloud Always Free, Google
Cloud Scheduler, AWS EventBridge Scheduler and the VPS free tiers. Their 2026
terms were not confirmed, and an unconfirmed free tier is not a fallback.

## Strand 2 — a beacon in the Mainline DHT (not proven)

The idea: the operator host publishes an Ed25519-signed heartbeat to a
permissionless bulletin board that nobody can turn off, and a second party
watches it and takes over the trigger when the heartbeat stops. That is
censorship resistance rather than redundancy: no account, domain or provider is
in the path, so nothing can be suspended.

**BEP-44 fits this unusually well, and that part is documented fact.** Mutable
items in the Mainline DHT are bound to an Ed25519 key pair; the key is
`SHA-1(public_key + salt)`, the publisher signs `seq + v` with a 64-byte
signature, and `seq` must increase monotonically, so a stale beacon cannot
overwrite a fresh one. This project already signs everything with Ed25519, so the
cryptography is not new — the transport is.

What was measured:

- **libtorrent 2.1.1.0 installs and runs here** (`pip install libtorrent`, macOS
  arm64). The session exposes `dht_put_mutable_item`, `dht_get_mutable_item`,
  `dht_put_immutable_item`, `dht_get_immutable_item`, `dht_announce`.
- **The DHT bootstraps, but not reliably.** Live node counts across four runs:
  60, 0, 41, 0, and 60 again. A channel that is empty half the time is not a
  coordination substrate for anything time-sensitive.
- **The Python binding could not be driven.** `dht_put_mutable_item` takes four
  `bytes` arguments and, for all six orderings of value, salt, public key and
  secret key, raises `ValueError: private key has wrong length, should be 64` —
  including the ordering in which the secret key is 64 bytes. The signature
  could not be determined empirically, so **no round trip was demonstrated.**

**Conclusion: do not build this on the Python binding.** If it is wanted, the
honest route is Rust — where this project already lives — against a Mainline DHT
crate, with the Ed25519 keys it already holds. The latency and bootstrap
behaviour above also bound what it is good for: a slow failover beacon, never a
one-minute trigger.

**And it only pays off if there are independent peers to notice.** Two machines
in one house are one failure domain; the DHT adds ceremony, not separation. This
strand is worth doing when the threat model is "my accounts and providers are
taken away", and not before.

## Strand 3 — distribute the backups with BitTorrent (proven)

`scripts/backup_recovery.py` produces one encrypted archive containing `d1.sql`
and `devnet-roles.json`, and today it lives on the NAS. That is a single
custodian on the operator's own hardware — the exact thing being removed.

BitTorrent fits this better than it fits scheduling, because the problems it
solves are the problems here: content-addressed integrity, chunked and resumable
transfer, no server required, and correctness verifiable by anyone holding the
magnet link. The archive is already AES-256-GCM encrypted and RSA-wrapped, so
peers never see plaintext.

**Verified by building one.** `torf` (pure Python, v4.3.1) creates a private
torrent from a file and emits a magnet URI directly:

```python
t = Torrent(path=archive, trackers=[],
            webseeds=["https://forecast.eastsea.xyz/backups/" + name])
t.generate()
```

That produced infohash `fc3d2982…` with the web seed carried in the magnet's
`ws=` parameter, and — because the torrent is not private — DHT and PEX stay
enabled, so it is trackerless. Two properties follow:

1. **A web seed removes the need for a seeder.** The archive can be served by the
   Cloudflare Worker that already exists, which is off the operator's system, so
   the data survives the NAS being off.
2. **Every peer that holds a copy is a custodian**, so the NAS stops being a
   single point of custody without anyone being asked to run anything.

`mktorrent -w <url>` does the same from the command line; `-p` must be avoided,
since private mode disables DHT and PEX.

**The open question is not technical, it is custodial:** a torrent is readable by
anyone with the magnet, and the archive's confidentiality rests entirely on the
NAS-held RSA wrapping key. Distributing the ciphertext to strangers is safe only
while that key stays off the network. Publishing the magnet publicly is therefore
a decision about the wrapping key's custody, not about bandwidth.

## What to do, in order

1. **Ping healthchecks.io from the operator and the sweep.** Free, five minutes
   of work, and it detects the exact failure that went unnoticed for two days.
2. **Confirm cron-job.org's real job limit, then add it as a redundant trigger.**
   It is the only free service found that offers a one-minute cadence, which is
   what the feed's 120-second expiry needs.
3. **Torrent the backups to at least two independent custodians**, web-seeded
   from Cloudflare — after deciding who may hold the wrapping key.
4. **Leave the DHT beacon alone** until there are independent peers and a threat
   model that needs it, and then build it in Rust.
