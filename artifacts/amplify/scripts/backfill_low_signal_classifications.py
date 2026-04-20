"""CLI: backfill low-signal classifications.

Walks artifacts/amplify/.classification_cache.json, finds rows whose title
alone is "obviously junk" (',etc.', '[Duplicate] ...', 'tbd', 'v16 -> v17',
sentinel words, or starts with punctuation), and rewrites them to the
deterministic guardrail result (importance 0, skip_reason="insufficient_input",
classification_method="guardrail_low_signal").

Usage:
    python -m scripts.backfill_low_signal_classifications              # dry run
    python -m scripts.backfill_low_signal_classifications --apply      # mutate
    python -m scripts.backfill_low_signal_classifications --cache PATH # custom cache path

Run from the artifacts/amplify directory so the local imports resolve.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai.classifier import (
    is_obviously_junk_title,
    _low_signal_result,
)


DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / ".classification_cache.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill low-signal classification cache rows.")
    parser.add_argument("--apply", action="store_true", help="Actually write the cache (default: dry run).")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH, help="Path to .classification_cache.json")
    parser.add_argument("--limit", type=int, default=50, help="Show up to this many sample rows (default 50).")
    args = parser.parse_args()

    cache_path: Path = args.cache
    if not cache_path.exists():
        print(f"[error] cache file not found: {cache_path}", file=sys.stderr)
        return 2

    with cache_path.open("r", encoding="utf-8") as f:
        cache = json.load(f)

    scanned = 0
    candidates = []

    for fid, cl in list(cache.items()):
        scanned += 1
        method = cl.get("classification_method", "")
        if method in ("quick_keyword", "guardrail_low_signal"):
            continue
        title = cl.get("title", "") or ""
        if not is_obviously_junk_title(title):
            continue
        candidates.append((fid, title, cl.get("importance_score", 0), method))

    print(f"Scanned: {scanned}")
    print(f"Would downgrade: {len(candidates)}")
    print()
    for fid, title, score, method in candidates[: args.limit]:
        title_disp = (title[:80] + "...") if len(title) > 80 else title
        print(f"  - [{method:>20}] score={score} {fid}  {title_disp!r}")
    if len(candidates) > args.limit:
        print(f"  ... and {len(candidates) - args.limit} more")
    print()

    if not args.apply:
        print("[dry-run] No changes written. Re-run with --apply to mutate the cache.")
        return 0

    if not candidates:
        print("Nothing to do.")
        return 0

    for fid, title, _score, _method in candidates:
        cache[fid] = _low_signal_result(fid, title)

    backup = cache_path.with_suffix(cache_path.suffix + ".bak")
    backup.write_text(cache_path.read_text(encoding="utf-8"), encoding="utf-8")
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    print(f"[apply] Wrote {len(candidates)} downgrades. Backup saved to {backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
