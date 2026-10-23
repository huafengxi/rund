#!/usr/bin/env python3
"""Run a single-writer scheduler as a normal foreground Service payload."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.scheduling import Scheduler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('--max-concurrent', type=int, default=4)
    args = parser.parse_args()
    scheduler = Scheduler(args.root,max_concurrent=args.max_concurrent)
    try:
        scheduler.run()
    finally:
        scheduler.close()


if __name__ == '__main__':
    main()
