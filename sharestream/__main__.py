"""``python -m sharestream`` entry point.

Runs the app under uvicorn using the host/port from config, preserving the
original ``--debug`` flag for verbose logging.
"""
from __future__ import annotations

import argparse
import logging
import warnings

import uvicorn
from passlib.exc import PasslibSecurityWarning

from sharestream.config import SHARESTREAM_HOST, SHARESTREAM_PORT
from sharestream.main import app

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Sharestream server.")
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")
    warnings.filterwarnings("ignore", category=PasslibSecurityWarning)
    uvicorn.run(app, host=SHARESTREAM_HOST, port=SHARESTREAM_PORT, access_log=False)


if __name__ == "__main__":
    main()
