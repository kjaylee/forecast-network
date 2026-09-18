#!/usr/bin/env python3
"""Turn an encrypted backup archive into a trackerless, web-seeded torrent.

`scripts/backup_recovery.py` already produces one AES-256-GCM encrypted,
RSA-wrapped archive. What it does not do is put a second copy anywhere that is
not the NAS, which is the operator's own hardware and therefore the thing being
removed from the critical path.

BitTorrent fits this better than it fits scheduling, because the problems it
solves are the problems here: content-addressed integrity, chunked and resumable
transfer, no server needed, and verification by anyone holding the magnet. A peer
that holds the archive holds ciphertext and nothing else.

The web seed is what removes the need for a seeder: point it at the Cloudflare
Worker that already exists and the archive survives the NAS being off.

**Custody, not bandwidth, is the decision.** Anyone with the magnet can fetch the
archive, and its confidentiality rests entirely on the NAS-held RSA wrapping key.
Do not distribute a magnet beyond the people allowed to hold that key.

Requires the `torf` package (pip install torf). It is a tooling dependency of
this script only, never of the service.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COMMENT = "forecast-network encrypted off-device backup (trackerless, web-seeded)"


def build(archive: Path, *, web_seed_base: str) -> tuple[Path, str, str]:
    """Write <archive>.torrent beside the archive. Returns (torrent path, infohash, magnet)."""
    try:
        from torf import Torrent
    except ImportError as error:  # pragma: no cover - depends on the operator's environment
        raise SystemExit("torf is required: python3 -m pip install torf") from error

    if not archive.is_file() or archive.stat().st_size == 0:
        raise SystemExit(f"not a readable, non-empty archive: {archive}")

    torrent = Torrent(
        path=str(archive),
        trackers=[],
        webseeds=[web_seed_base.rstrip("/") + "/" + archive.name],
        comment=COMMENT,
    )
    # No tracker, and deliberately not private: private mode disables the DHT and
    # peer exchange, which are the only ways anyone finds this without a tracker.
    torrent.private = False
    torrent.generate()
    if torrent.private:
        raise SystemExit("refusing to write a torrent with private mode set")

    destination = archive.with_suffix(archive.suffix + ".torrent")
    # torf refuses to overwrite by default. Re-deriving the torrent for an unchanged
    # archive is the same torrent, so re-publishing is deliberately idempotent.
    torrent.write(str(destination), overwrite=True)
    return destination, str(torrent.infohash), str(torrent.magnet())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="The encrypted archive to distribute")
    parser.add_argument("--web-seed-base", required=True,
                        help="Base URL that will serve the archive, e.g. https://forecast.eastsea.xyz/backups")
    args = parser.parse_args()
    destination, infohash, magnet = build(args.archive, web_seed_base=args.web_seed_base)
    print(json.dumps({"event": "backup_torrent", "torrent": str(destination),
                      "infohash": infohash, "webSeeded": True, "magnet": magnet}, sort_keys=True))
    print("\nDistributing this magnet publishes the archive to anyone who reads it.\n"
          "Its confidentiality rests on the NAS-held wrapping key staying off the network.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
