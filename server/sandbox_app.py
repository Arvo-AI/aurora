"""Local sandbox runner without PostgreSQL, Redis, Vault or customer connectors.

Production uses the same blueprint registered in main_compute.py. This runner
binds to loopback only and uses an explicitly process-local development limit.
"""

from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_limiter import Limiter

from routes.sandbox_routes import sandbox_bp  # noqa: E402


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 512
    limiter = Limiter(key_func=lambda: "sandbox-investigations", storage_uri="memory://")
    limiter.init_app(app)
    limiter.shared_limit("9 per minute;90 per hour;300 per day", scope="sandbox-model-work", methods=["POST"])(sandbox_bp)
    app.register_blueprint(sandbox_bp)

    @app.errorhandler(429)
    def limit_reached(_error):
        return jsonify({"error": "The sample investigation budget has been reached. Try again later."}), 429

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"error": "Request is too large."}), 413

    return app


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    create_app().run(host="127.0.0.1", port=5080, threaded=True, debug=False)
