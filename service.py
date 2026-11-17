"""Development transport only: explicit CLI opt-in, loopback default, no admin API."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import faulthandler
import signal

from engine.controller import Controller
from engine.budget import BudgetError
from engine.security import SecurityError
from engine.launching import Launcher, LaunchError
from engine.research import Research


def make_server(controller, address=("127.0.0.1", 8765)):
    launcher = Launcher(controller)
    research = Research(launcher)
    class Server(ThreadingHTTPServer):
        def service_actions(self):
            controller.autofinalize_due()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log tokens, sources or URL contents.

        def do_POST(self):
            status = 200
            try:
                match = re.fullmatch(r"/v1/(sessions|launchers)/([0-9a-f]{32})/([a-z_]+)", self.path)
                if not match:
                    raise ValueError("Unknown API route")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16 * 1024 * 1024:
                    raise ValueError("Request body size is invalid")
                token = self.headers.get("Authorization", "")
                if not token.startswith("Bearer "):
                    raise BudgetError("AUTH_FAILED", "Session bearer token is required")
                body = json.loads(self.rfile.read(size))
                if match[1] == 'sessions':
                    result = controller.dispatch(match[2], token[7:], match[3], body)
                elif match[3] == 'open' and isinstance(body, dict) and set(body) == {'track', 'request_id'}:
                    result = launcher.open(match[2], token[7:], **body)
                elif match[3] == 'history' and body == {}:
                    result = launcher.history(match[2], token[7:])
                elif match[3] == 'tasks' and body == {}:
                    result = research.catalog(match[2], token[7:])
                elif match[3] == 'open_task' and isinstance(body, dict) and set(body) == {'task_id', 'track', 'request_id'}:
                    result = research.open_task(match[2], token[7:], **body)
                elif match[3] == 'experiment_create' and isinstance(body, dict) and set(body) == {'request_id', 'plan'}:
                    result = research.create(match[2], token[7:], **body)
                elif match[3] == 'experiment_open' and isinstance(body, dict) and set(body) == {'experiment_id', 'trial_id'}:
                    result = research.open_trial(match[2], token[7:], **body)
                elif match[3] == 'experiment_finish' and isinstance(body, dict) and set(body) == {'experiment_id', 'trial_id'}:
                    result = research.finish_trial(match[2], token[7:], **body)
                elif match[3] == 'experiment_report' and isinstance(body, dict) and set(body) == {'experiment_id'}:
                    result = research.report(match[2], token[7:], **body)
                else:
                    raise ValueError('Unknown launcher command or unsupported fields')
            except (BudgetError, SecurityError, LaunchError) as error:
                status = 409 if error.code != "AUTH_FAILED" else 401
                result = {"error": error.code, "message": str(error)}
                if isinstance(error, SecurityError):
                    result["security"] = error.report
            except (ValueError, TypeError, KeyError) as error:
                status, result = 400, {"error": "INVALID_REQUEST", "message": str(error)}
            except Exception:
                status, result = 500, {"error": "JUDGE_UNAVAILABLE", "message": "Trusted administrator must inspect the judge"}
            data = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Server(address, Handler)


def main():
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--database", help="Node-local authoritative SQLite path, bound at initial setup")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", choices=("development", "formal"), default="formal")
    parser.add_argument("--allow-unisolated-development", action="store_true")
    args = parser.parse_args()
    if args.mode == "development" and not args.allow_unisolated_development:
        parser.error("Development executes candidate code without OS isolation; explicitly enable it for trusted smoke tests")
    server = make_server(Controller(args.state, mode=args.mode, database=args.database), ("127.0.0.1", args.port))
    print(json.dumps({"listening": server.server_address, "mode": args.mode,
                      "formal_backend_available": False}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
