"""Entry point for hosting (PythonAnywhere, Render, etc.).

PythonAnywhere: in the Web tab, open the WSGI configuration file and replace its
contents with the 4 lines at the bottom of DEPLOY.md (they import `app` from here).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, init_db, seed_demo  # noqa: E402

init_db()
seed_demo()
application = app          # name most hosts look for
