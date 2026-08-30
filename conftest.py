"""Ensures the project root is on sys.path so tests/ can `import monitor_bot`."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
