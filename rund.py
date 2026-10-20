#!/usr/bin/env python3
"""Caller-configured local runtime and services command line."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from runtime.service_cli import main
if __name__=='__main__':sys.exit(main())
