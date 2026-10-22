#!/usr/bin/env python3
"""Run a single-writer scheduler as a normal foreground Service payload."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.scheduling import Scheduler
from runtime.store import read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    scheduler = Scheduler(read(args.config))
    try:
        scheduler.run()
    finally:
        scheduler.close()


if __name__ == '__main__':
    main()
