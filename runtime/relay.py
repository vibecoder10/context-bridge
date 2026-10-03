"""Small authenticated, durable inbox. Python standard library only."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_TEXT = 12000


class BridgeError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(temp, path)


def initialize(state, url):
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (state / "relay.json").exists():
        raise BridgeError("State already initialized; use the existing configuration.")
    project = str(uuid.uuid4())
    identities = {name: secrets.token_urlsafe(32) for name in ("ryan", "customer")}
    private_json(state / "relay.json", {"project_id": project, "identities": identities, "max_messages": 8})
    for name, token in identities.items():
        private_json(state / (name + ".json"), {
            "project_id": project, "relay_url": url, "identity": name, "token": token,
            "thread_id": None, "cwd": str(state / (name + "-workspace")),
        })
    return state


class Inbox:
    def __init__(self, config, database):
        self.config = config
        self.database = str(database)
        if len(config["identities"]) != 2 or len(set(config["identities"].values())) != 2:
            raise BridgeError("Exactly two distinct identities and credentials are required.")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations(
                  id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                  sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL,
                  kind TEXT NOT NULL, round INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                  lease TEXT, lease_until REAL, created REAL NOT NULL, completed REAL);
                CREATE TABLE IF NOT EXISTS requests(
                  identity TEXT, key TEXT, digest TEXT NOT NULL, result TEXT NOT NULL,
                  PRIMARY KEY(identity,key));
            """)
        os.chmod(self.database, 0o600)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def identity(self, header):
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        for identity, expected in self.config["identities"].items():
            if hmac.compare_digest(token, expected):
                return identity
        raise BridgeError("Invalid bridge credential.", 401)

    def peer(self, identity):
        return next(name for name in self.config["identities"] if name != identity)

    def text(self, body):
        value = body.get("text")
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
            raise BridgeError("Message must contain 1 to 12000 characters.")
        return value

    def public(self, row):
        return {key: row[key] for key in (
            "id", "conversation_id", "sender", "recipient", "text", "kind", "round", "state", "created"
        )}

    def request(self, identity, action, body):
        if body.get("project_id") != self.config["project_id"]:
            raise BridgeError("Wrong project.", 403)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if action == "inbox":
                rows = db.execute("SELECT * FROM messages WHERE recipient=? AND state!='done' ORDER BY created LIMIT 50", (identity,)).fetchall()
                return {"messages": [self.public(row) for row in rows]}
            if action == "claim":
                row = db.execute("SELECT * FROM messages WHERE recipient=? AND (state='queued' OR (state='processing' AND lease_until<?)) ORDER BY created LIMIT 1", (identity, time.time())).fetchone()
                if row is None:
                    return {"message": None}
                lease = str(uuid.uuid4())
                db.execute("UPDATE messages SET state='processing',lease=?,lease_until=? WHERE id=?", (lease, time.time() + 600, row["id"]))
                return {"message": dict(self.public(row), lease=lease)}
            if action == "transcript":
                conversation = body.get("conversation_id")
                record = db.execute("SELECT * FROM conversations WHERE id=?", (conversation,)).fetchone()
                if record is None:
                    raise BridgeError("Discussion not found.", 404)
                rows = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY round", (conversation,)).fetchall()
                return {"conversation": dict(record), "messages": [self.public(row) for row in rows]}
            key = body.get("idempotency_key")
            if not isinstance(key, str) or not 1 <= len(key) <= 128:
                raise BridgeError("An idempotency key is required.")
            digest = hashlib.sha256(json.dumps([action, body], sort_keys=True).encode()).hexdigest()
            previous = db.execute("SELECT * FROM requests WHERE identity=? AND key=?", (identity, key)).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise BridgeError("Idempotency key reused with different content.", 409)
                return json.loads(previous["result"])
            now = time.time()
            if action == "send":
                content = self.text(body)
                conversation = str(uuid.uuid4())
                db.execute("INSERT INTO conversations(id,created) VALUES(?,?)", (conversation, now))
                message_id = self.insert(db, conversation, identity, content, "question", 1, now)
                result = {"conversation_id": conversation, "message_id": message_id, "status": "queued"}
            elif action == "complete":
                row = db.execute("SELECT * FROM messages WHERE id=? AND recipient=?", (body.get("message_id"), identity)).fetchone()
                if row is None:
                    raise BridgeError("Message is not addressed to this identity.", 403)
                if row["state"] != "processing" or row["lease"] != body.get("lease") or row["lease_until"] < now:
                    raise BridgeError("Message lease is not active.", 409)
                conversation = row["conversation_id"]
                if row["kind"] == "final":
                    result = {"conversation_id": conversation, "status": "complete"}
                    db.execute("UPDATE conversations SET status='complete' WHERE id=? AND status='open'", (conversation,))
                else:
                    content = self.text(body)
                    needs_reply = body.get("needs_reply")
                    if type(needs_reply) is not bool:
                        raise BridgeError("needs_reply must be a boolean.")
                    limit = row["round"] + 1 >= self.config["max_messages"]
                    kind = "final" if limit or not needs_reply else "question"
                    message_id = self.insert(db, conversation, identity, content, kind, row["round"] + 1, now)
                    if limit and needs_reply:
                        db.execute("UPDATE conversations SET status='limit_reached' WHERE id=?", (conversation,))
                    result = {"conversation_id": conversation, "message_id": message_id,
                              "status": "limit_reached" if limit and needs_reply else "queued", "kind": kind}
                db.execute("UPDATE messages SET state='done',completed=?,lease=NULL,lease_until=NULL WHERE id=?", (now, row["id"]))
            else:
                raise BridgeError("Unknown action.", 404)
            db.execute("INSERT INTO requests VALUES(?,?,?,?)", (identity, key, digest, json.dumps(result)))
            return result

    def insert(self, db, conversation, sender, content, kind, round_number, now):
        message_id = str(uuid.uuid4())
        db.execute("INSERT INTO messages(id,conversation_id,sender,recipient,text,kind,round,created) VALUES(?,?,?,?,?,?,?,?)",
                   (message_id, conversation, sender, self.peer(sender), content, kind, round_number, now))
        return message_id


def server(inbox, host="127.0.0.1", port=8787):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log Authorization headers or message bodies.

        def respond(self, value, status=200):
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.respond({"status": "ready"} if self.path == "/health" else {"error": "Not found"}, 200 if self.path == "/health" else 404)

        def do_POST(self):
            try:
                identity = inbox.identity(self.headers.get("Authorization", ""))
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100000:
                    raise BridgeError("Invalid request size.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise BridgeError("Request must be a JSON object.")
                self.respond(inbox.request(identity, self.path.removeprefix("/"), body))
            except BridgeError as error:
                self.respond({"error": str(error)}, error.status)
            except (ValueError, TypeError, KeyError):
                self.respond({"error": "Invalid request."}, 400)
            except Exception:
                self.respond({"error": "Internal relay error."}, 500)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "serve"])
    parser.add_argument("--state", default=".state")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    state = Path(args.state).resolve()
    if args.command == "init":
        initialize(state, "http://127.0.0.1:%d" % args.port)
        print("Created separate client credentials in %s. Credentials were not printed." % state)
    else:
        config = json.loads((state / "relay.json").read_text())
        app = server(Inbox(config, state / "inbox.sqlite3"), port=args.port)
        print("Bridge listening on loopback port %d" % app.server_address[1], flush=True)
        try:
            app.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.server_close()


if __name__ == "__main__":
    main()
