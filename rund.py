#!/usr/bin/env python3
"""Direct foreground node runtime."""
import argparse
from pathlib import Path
from runtime.config import node_config
from runtime.engine import Engine


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path.home()/'.rund')
    args=parser.parse_args()
    engine=Engine(node_config(args.root))
    try:engine.run()
    finally:engine.close()


if __name__=='__main__':main()
