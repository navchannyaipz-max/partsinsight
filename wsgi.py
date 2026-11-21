"""Точка входу для WSGI-хостингів (PythonAnywhere, gunicorn): `gunicorn wsgi:app`."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run import app  # noqa: E402,F401
