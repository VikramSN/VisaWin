"""
passenger_wsgi.py — Entry point for GoDaddy cPanel Phusion Passenger.
"""

import sys
import os

# Ensure current directory is at the top of sys.path
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from app import app as application
