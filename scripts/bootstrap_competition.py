#!/usr/bin/env python3
"""Bootstrap helper for official competition materials ingestion.

Orchestrates authoritative source bundle creation by delegating directly to
Battlelab's existing official ingestion subsystem (battlelab.official.sources.ingest_sources).

Derives the single canonical source bundle hash, enforces fail-closed provenance
and symlink rejection, and prints next-step commands for typed game-spec initialization.

Does NOT maintain a competing inventory or staging system.
Does NOT activate adapters or perform submissions.
Does NOT modify source files or execute network requests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from battlelab.official.sources import ingest_sources


def bootstrap_competition(
    source_path: Path | str,
    copy_files: bool = True,
    bundles_dir: Path | None = None,
) -> dict[str, Any]:
    """Ingest official materials through Battlelab's canonical official subsystem.

    Returns the authoritative SourceBundleManifest dictionary.
    Rejects missing paths, empty directories, symlinks, and path traversal.
    """
    path = Path(source_path)
    # Delegates directly to battlelab.official.sources.ingest_sources
    manifest = ingest_sources(
        source_path=path,
        copy_files=copy_files,
        bundles_dir=bundles_dir,
    )
    return manifest.to_dict()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap official competition materials via Battlelab official ingestion subsystem."
    )
    parser.add_argument(
        "path",
        help="Path to file or directory containing authoritative official competition materials.",
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Do not copy source files into the content-addressed bundle (default: copy).",
    )
    parser.add_argument(
        "--bundles-dir",
        default=None,
        help="Optional custom root directory for source bundles (default: data/official/source_bundles).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output authoritative source bundle manifest JSON to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    src_path = Path(args.path)

    if not src_path.exists():
        sys.stderr.write(f"Error: Official materials path does not exist: {src_path}\n")
        return 1

    custom_bundles = Path(args.bundles_dir) if args.bundles_dir else None
    copy_files = not args.no_copy

    try:
        manifest_dict = bootstrap_competition(
            source_path=src_path,
            copy_files=copy_files,
            bundles_dir=custom_bundles,
        )
    except Exception as e:
        sys.stderr.write(f"Error ingesting official materials: {e}\n")
        return 1

    if args.json:
        sys.stdout.write(json.dumps(manifest_dict, indent=2, sort_keys=True) + "\n")
        return 0

    bundle_hash = manifest_dict["bundle_hash"]
    file_count = manifest_dict["file_count"]
    total_bytes = manifest_dict["total_size_bytes"]

    print("=" * 70)
    print("BATTLELAB OFFICIAL COMPETITION MATERIALS INGESTION")
    print("=" * 70)
    print(f"Canonical Source Bundle Hash: {bundle_hash}")
    print(f"Source Path:                  {manifest_dict['source_path']}")
    print(f"Files Cataloged:              {file_count}")
    print(f"Total Bytes:                  {total_bytes}")
    print(
        f"Manifest Location:            data/official/source_bundles/{bundle_hash}/source_manifest.json"
    )
    print("-" * 70)
    print("Authoritative File Records:")
    for f in manifest_dict.get("files", [])[:10]:
        sha_short = f["sha256"][:12]
        print(
            f"  [{f['media_type']:<22}] {f['relpath']:<32} ({f['size_bytes']:>8} B, sha256:{sha_short}...)"
        )
    if file_count > 10:
        print(f"  ... and {file_count - 10} more files.")

    print("\nNext Official Steps:")
    print("  1. Verify bundle status:")
    print("     python -m battlelab official status")
    print(
        "  2. Initialize typed 23-rule game specification (referencing docs/game_spec.template.yaml):"
    )
    print(
        f"     python -m battlelab official spec init --source-bundle {bundle_hash} --output configs/game_spec.yaml"
    )
    print("  3. Populate rule meanings and citations in configs/game_spec.yaml, then validate:")
    print("     python -m battlelab official spec validate configs/game_spec.yaml")
    print("  4. Probe official SDK:")
    print("     python -m battlelab official sdk probe")
    print("  5. Inspect overall launch readiness:")
    print("     python -m battlelab competition status")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
