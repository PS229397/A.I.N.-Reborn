#!/usr/bin/env python3
"""
Drop-in shim — delegates to ain.pipeline.

Install the package for the full `ain` CLI:
    pip install ain-pipeline

Or use this file directly in any repo without installing:
    python pipeline.py init
    python pipeline.py run
    python pipeline.py --status
"""
from ain.pipeline import main

if __name__ == "__main__":
    main()
