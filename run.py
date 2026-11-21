from __future__ import annotations
import os
port = int(os.environ.get("PORT", 5000))
os.environ.setdefault("BASE_URL", f"http://127.0.0.1:{port}")

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=port, threaded=True)
