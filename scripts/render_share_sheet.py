#!/usr/bin/env python3
"""Render a recovery key share as a sheet to print and keep somewhere else.

The last step of splitting the recovery key is physical: one share has to leave
the house, or a fire takes all of them and the archives become unopenable. That
step keeps not happening because a file on a disk is not something you can put
in a drawer.

This turns one share into a single printable page — what it is, which key it
belongs to, how many are needed, and the value itself — so the physical step is
printing and walking away with it. Whoever finds it in ten years needs to know
what it is without asking anyone.

    python3 scripts/render_share_sheet.py --share <share.json> --out <sheet.html>

A share below the threshold reveals nothing about the key, so printing it is
safe. Transcription errors are caught later: every share carries the key's
fingerprint, and `combine` refuses a mixed, short or tampered set rather than
returning a wrong key.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recovery_key_shares import decode_share  # noqa: E402

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Forecast Network recovery key share {index} of {shares}</title>
<style>
  body {{ font: 12pt/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; margin: 24mm 18mm; color: #111; }}
  h1 {{ font-size: 16pt; margin: 0 0 2mm; }}
  .lede {{ color: #444; margin: 0 0 8mm; }}
  table {{ border-collapse: collapse; margin-bottom: 8mm; }}
  th, td {{ text-align: left; padding: 1mm 6mm 1mm 0; vertical-align: top; }}
  th {{ font-weight: 600; white-space: nowrap; }}
  .value {{ font: 9pt/1.4 ui-monospace, Menlo, Consolas, monospace; word-break: break-all;
            border: 1px solid #999; padding: 4mm; background: #fafafa; }}
  .warn {{ border-left: 3px solid #b00; padding-left: 4mm; margin-top: 8mm; color: #111; }}
  ol {{ padding-left: 6mm; }}
  @media print {{ body {{ margin: 16mm; }} .noprint {{ display: none; }} }}
</style></head><body>
<h1>Forecast Network — recovery key share {index} of {shares}</h1>
<p class="lede">This is one of {shares} pieces of the key that opens the encrypted
off-device backups. <strong>{threshold} of them</strong> are needed; this one alone
opens nothing, which is why it is safe to keep here.</p>

<table>
  <tr><th>Key fingerprint</th><td><code>{fingerprint}</code></td></tr>
  <tr><th>This share</th><td>{index} of {shares}</td></tr>
  <tr><th>Needed to rebuild</th><td>{threshold} shares</td></tr>
  <tr><th>Backups it opens</th><td>poc-nas:/home/spritz/AI/forecast-network/backups/</td></tr>
</table>

<p>The value below is the share. Keep it with this page.</p>
<div class="value">{value}</div>

<div class="warn">
<p><strong>Do not keep this with another share.</strong> Any {threshold} shares
together rebuild the key. If this page is stored beside two others, the place it
is stored can open every backup.</p>
</div>

<h2 style="font-size:13pt;margin:8mm 0 3mm">Rebuilding the key, if it is ever needed</h2>
<ol>
  <li>Save each of the {threshold} shares as its own file.</li>
  <li><code>python3 scripts/recovery_key_shares.py combine --share a.json --share b.json --share c.json --out rebuilt.pem</code></li>
  <li>The command refuses a set that is mixed, short or mistyped, and prints the
      fingerprint it rebuilt. It must match the one on this page.</li>
</ol>
<p class="lede noprint">Printed from a machine that holds a share; this page and
the machine should not stay in the same place.</p>
</body></html>
"""


def render(share: Path) -> str:
    index, _values, meta = decode_share(share.read_text())
    return PAGE.format(
        index=index,
        shares=html.escape(str(meta["shares"])),
        threshold=html.escape(str(meta["threshold"])),
        fingerprint=html.escape(str(meta["fingerprint"])),
        value=html.escape(json.dumps(json.loads(share.read_text()), sort_keys=True)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--share", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(render(args.share))
    print(json.dumps({"event": "share_sheet_rendered", "share": str(args.share),
                      "out": str(args.out)}, sort_keys=True))
    print("\nPrint it and store it somewhere that is not this machine and not the NAS.\n"
          "A share below the threshold opens nothing on its own.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
