#!/usr/bin/env python3
"""
Servidor local: serve public/ e a mesma API da Vercel (/api/agent).
Uso: python3 server.py [porta]      (lê GROQ_API_KEY do ambiente ou de um arquivo .env)
"""
import gzip
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"

env = ROOT / ".env"
if env.exists():
    for linha in env.read_text().splitlines():
        if "=" in linha and not linha.strip().startswith("#"):
            k, v = linha.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

sys.path.insert(0, str(ROOT / "api"))
import agent  # noqa: E402


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def _send(self, code, raw, ctype):
        gz = "gzip" in self.headers.get("Accept-Encoding", "") and len(raw) > 1024
        if gz:
            raw = gzip.compress(raw, compresslevel=5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, data):
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_POST(self):
        if not self.path.startswith("/api/agent"):
            return self._json(404, {"ok": False, "error": "Endpoint não encontrado"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            res = agent.processar(payload, {"X-LLM-Key": self.headers.get("X-LLM-Key")})
            self._json(200 if res.get("ok") else 400, res)
        except ValueError as e:
            self._json(404, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/agent"):
            return self._json(200, agent.status())
        if path.endswith(".json"):
            f = PUBLIC_DIR / path.lstrip("/")
            if f.is_file():
                return self._send(200, f.read_bytes(), "application/json; charset=utf-8")
        return super().do_GET()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8080))
    print(f"Atendimento AI-First em http://localhost:{port}  (LLM: {agent.status()['provedor'] or 'desligado, modo regras'})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
