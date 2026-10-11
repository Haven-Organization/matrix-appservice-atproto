"""Entrypoint: load config.yaml and run the bridge's HTTP server with uvicorn.

Run natively, no containerization:

    python main.py [path/to/config.yaml]
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from bridge.config import ConfigError, load_config
from bridge.server import create_app

logger = logging.getLogger(__name__)


def main(argv: list[str]) -> int:
    config_path = argv[0] if argv else None
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=config.logging.level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = create_app(config)
    # log_config=None: let uvicorn's loggers propagate to (and respect the
    # level of) the root logger configured above, instead of uvicorn's own
    # hardcoded INFO.
    #
    # timeout_graceful_shutdown: a relay holds its firehose WebSocket open
    # forever, and uvicorn by default waits for every connection to finish
    # before exiting -- so without a limit, stopping the bridge hangs until
    # systemd gives up and kills it. Consumers reconnect with their cursor.
    uvicorn.run(
        app,
        host=config.bridge.listen_host,
        port=config.bridge.listen_port,
        log_config=None,
        timeout_graceful_shutdown=5,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
