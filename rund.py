#!/usr/bin/env python3
"""Node runtime payload, executed as an ordinary Service Task."""
import argparse
from pathlib import Path
from runtime.config import node_config
from runtime.engine import Engine


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    engine=Engine(node_config(args.config))
    try:engine.run()
    finally:engine.close()


if __name__=='__main__':main()
