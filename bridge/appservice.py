"""Matrix AppService registration file generator.

Synapse needs a registration YAML (referenced from its ``homeserver.yaml``
via ``app_service_config_files``) describing this bridge: its tokens, the
namespace of virtual users it owns, and the URL Synapse should push events
to. This module builds that file from ``config.yaml`` so the two never
drift out of sync.

Usage::

    python -m bridge.appservice config.yaml appservice-registration.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from bridge.config import BridgeConfig, load_config


def build_registration(config: BridgeConfig) -> dict[str, Any]:
    """Build the registration document as a plain dict (ready to dump as YAML)."""
    as_section = config.appservice
    return {
        "id": as_section.id,
        "url": config.bridge.resolved_internal_base_url(),
        "as_token": as_section.as_token,
        "hs_token": as_section.hs_token,
        "sender_localpart": as_section.bot_localpart,
        "rate_limited": False,
        "namespaces": {
            # No ":server" suffix on either regex: Synapse matches these as
            # an unanchored prefix against the full MXID, and including the
            # domain has been seen to stop it pushing any events at all.
            "users": [
                {"exclusive": True, "regex": f"@{as_section.user_prefix}.*"},
                {"exclusive": True, "regex": f"@{as_section.bot_localpart}"},
            ],
            "aliases": [],
            "rooms": [],
        },
    }


def write_registration(config: BridgeConfig, output_path: str | Path) -> Path:
    """Build and write the registration YAML, returning the path written."""
    path = Path(output_path)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(build_registration(config), fh, default_flow_style=False, sort_keys=False)
    return path


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print("Usage: python -m bridge.appservice [config.yaml] [output_path]", file=sys.stderr)
        return 2
    config = load_config(argv[0] if argv else None)
    written = write_registration(config, argv[1] if len(argv) == 2 else config.appservice.registration_path)
    print(f"Wrote AppService registration to {written}")
    print("Add this path to your homeserver.yaml's `app_service_config_files` list and restart Synapse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
