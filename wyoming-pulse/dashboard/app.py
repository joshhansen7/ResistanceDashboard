"""
Wyoming Pulse — Dashboard Flask Application
Serves the web dashboard for sentiment visualization.
"""

import os
import sys
import time
from pathlib import Path

from flask import Flask, render_template

# Ensure the project root is on the path so we can import db.py
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def create_app():
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )

    app.config["DB_PATH"] = str(PROJECT_ROOT / "data" / "wyoming_pulse.db")

    from . import api
    app.register_blueprint(api.bp)

    @app.route("/")
    def index():
        return render_template("index.html", cache_bust=int(time.time()))

    return app


def start_dashboard(host="127.0.0.1", port=5000):
    """Start the dashboard server."""
    # Allow PORT env var override (used by preview tools)
    port = int(os.environ.get("PORT", port))
    app = create_app()
    print(f"\n📊 Wyoming Pulse Dashboard")
    print(f"   Running at http://{host}:{port}")
    print(f"   Press Ctrl+C to stop\n")
    app.run(host=host, port=port, debug=False)
