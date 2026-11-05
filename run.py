"""Local runner: python run.py [--host 0.0.0.0] [--port 8000]

Loads optional .env (PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY, ...) and stores data in ./data/inventory.db.
"""
import argparse
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


if __name__ == "__main__":
    load_env(os.path.join(ROOT, ".env"))
    os.environ.setdefault("DB_PATH", os.path.join(ROOT, "data", "inventory.db"))

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    import uvicorn

    os.chdir(ROOT)
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
