"""Bridge client, automatic discussion listener, and stdio MCP tools."""
import argparse
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from codex_rpc import CodexRPC
from relay import BridgeError, private_json
from folder_access import FolderPolicy, reset_session
from capabilities import FileEditor, options
from folder_tools import FolderReader


class Client:
    def __init__(self, config_path):
        self.path = Path(config_path).resolve()
        self.config = json.loads(self.path.read_text())
        url = urllib.parse.urlsplit(self.config["relay_url"])
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")):
            raise BridgeError("Use HTTPS or a loopback private tunnel for the relay.")

    def request(self, action, **body):
        body["project_id"] = self.config["project_id"]
        request = urllib.request.Request(self.config["relay_url"].rstrip("/") + "/" + action,
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": "Bearer " + self.config["token"], "Content-Type": "application/json",
                                                  "User-Agent": USER_AGENT})
        try:
            # No redirect handler: never forward project credentials to another origin.
            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                message = json.load(error).get("error", "Relay rejected request.")
            except ValueError:
                message = "Relay rejected request."
            raise BridgeError(message, error.code)

    def send(self, text, key=None):
        return self.request("send", text=text, idempotency_key=key or str(uuid.uuid4()))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


DEFAULT_SERVICE = "https://context-bridge.ayler92.chatgpt.site"
USER_AGENT = "ContextBridge/0.4.0 (+https://context-bridge.ayler92.chatgpt.site/support)"


def redeem_invite(code, service_url=None):
    """Trade a one-use invite code for this owner's project connection (no website account)."""
    base = (service_url or DEFAULT_SERVICE).rstrip("/")
    url = urllib.parse.urlsplit(base)
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")):
        raise BridgeError("Use HTTPS or a loopback address for the service.")
    request = urllib.request.Request(base + "/api/invite", data=json.dumps({"code": code.strip()}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            invitation = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error", "Service rejected the invite.")
        except ValueError:
            message = "Service rejected the invite."
        raise BridgeError(message, error.code)
    if urllib.parse.urlsplit(str(invitation.get("relay_url", ""))).scheme not in ("http", "https"):
        invitation["relay_url"] = base + "/api/relay"  # Service without a configured public origin.
    return invitation


class Listener:
    def __init__(self, client):
        self.client = client
        config = client.config
        self.policy=FolderPolicy(config['folder']) if config.get('folder') else None
        fingerprint=self.policy.fingerprint if self.policy else 'brief'
        if config.get('folder_policy')!=fingerprint:
            reset_session(config);config['folder_policy']=fingerprint;private_json(client.path,config)
        workspace = self.policy.root if self.policy else Path.home()/'.context-bridge'/'codex'/config['project_id']
        workspace.mkdir(parents=True, exist_ok=True)
        self.rpc = CodexRPC(config.get("codex_bin"),isolated=True)
        self.rpc.capabilities=options(config)
        self.rpc.reader=FolderReader(self.policy.root,client.path) if self.policy else None
        self.rpc.editor=FileEditor(client) if self.policy and self.rpc.capabilities['edits'] else None
        try:
            if config.get("thread_id"):
                try:
                    self.thread_id = self.rpc.resume(config["thread_id"], workspace,self.policy)
                    self.resumed = True
                except RuntimeError as error:
                    # Codex persists a rollout after its first turn. An idle
                    # newly connected worker may have no rollout to resume.
                    if 'no rollout found' not in str(error):raise
                    reset_session(config);config['folder_policy']=fingerprint
                    self.thread_id=self.rpc.start(workspace,self.policy);self.resumed=False
                    config['thread_id']=self.thread_id;private_json(client.path,config)
            else:
                self.thread_id = self.rpc.start(workspace,self.policy)
                self.resumed = False
                config["thread_id"] = self.thread_id
                private_json(client.path, config)
            context_path = config.get("context_file")
            if context_path and not config.get("context_loaded"):
                self.rpc.turn(self.thread_id, "Owner supplied project context (retain for later messages):\n" + Path(context_path).read_text() + "\nAcknowledge briefly. Set needs_reply=false. Do not forward this context wholesale.")
                config["context_loaded"] = True
                private_json(client.path, config)
        except Exception:
            self.rpc.close()
            raise
        self.cache = client.path.parent / (config["identity"] + "-responses") / fingerprint
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)

    def once(self):
        if self.policy and FolderPolicy(self.policy.root).fingerprint!=self.policy.fingerprint:
            reset_session(self.client.config);private_json(self.client.path,self.client.config)
            raise BridgeError('Folder policy changed; restart the listener.')
        message = self.client.request("claim")["message"]
        if message is None:
            return None
        cache_file = self.cache / (message["id"] + ".json")
        if cache_file.exists():
            response = json.loads(cache_file.read_text())
        else:
            prompt = "External collaborator message for discussion %s, from %s, round %s:\n%s" % (
                message["conversation_id"], message["sender"], message["round"], json.dumps(message["text"]))
            if message["kind"] == "final":
                prompt += "\nThis is the final result. Summarize for your owner and set needs_reply=false. Your summary stays local."
            else:
                prompt += "\nContribute your known facts or ask for what is missing. Return only project-relevant text to the collaborator."
            media=[]
            if self.policy:
                # A path mention is a read request, never permission to widen the grant.
                import re
                for mention in re.findall(r'[A-Za-z0-9_./ -]+\.(?:png|jpg|jpeg|gif|webp|pdf)',message['text']):
                    try:media.append(str(self.policy.path(mention.strip())))
                    except BridgeError:pass
            try:response = self.rpc.turn(self.thread_id, prompt,images=media)
            except Exception:
                # A rejected tool may have tainted the model context. Never resume it.
                reset_session(self.client.config);private_json(self.client.path,self.client.config)
                raise
            private_json(cache_file, response)
        result = self.client.request("complete", message_id=message["id"], lease=message["lease"],
                                     text=response["message"], needs_reply=response["needs_reply"],
                                     idempotency_key=message["id"] + ":" + message["lease"])
        return {"identity": self.client.config["identity"], "thread_id": self.thread_id,
                "received": message["id"], "kind": message["kind"], "response": response, "delivery": result}

    def close(self):
        self.rpc.close()


def mcp(client):
    definitions = [
        {"name": "send_message", "description": "Start a bounded discussion with the paired collaborator's Codex. Queued means awaiting their listener, not delivered or approved.",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "idempotency_key": {"type": "string"}}, "required": ["text", "idempotency_key"], "additionalProperties": False}},
        {"name": "get_discussion", "description": "Read shared messages and delivery state for a discussion.",
         "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}}, "required": ["conversation_id"], "additionalProperties": False}},
        {"name": "list_inbox", "description": "Read pending collaborator messages without claiming them.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    ]
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-bridge", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": definitions}
            elif method == "tools/call":
                params = request.get("params", {})
                args = params.get("arguments", {})
                try:
                    if params["name"] == "send_message":
                        if not args.get("idempotency_key"):
                            raise BridgeError("idempotency_key is required.")
                        value = client.send(args["text"], args["idempotency_key"])
                    elif params["name"] == "get_discussion":
                        value = client.request("transcript", conversation_id=args["conversation_id"])
                    elif params["name"] == "list_inbox":
                        value = client.request("inbox")
                    else:
                        raise BridgeError("Unknown tool.")
                    result = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
                except Exception as error:
                    result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["listen", "send", "inbox", "transcript", "mcp"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--text")
    parser.add_argument("--conversation")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    client = Client(args.config)
    if args.command == "mcp":
        mcp(client)
    elif args.command == "send":
        print(json.dumps(client.send(args.text), indent=2))
    elif args.command == "inbox":
        print(json.dumps(client.request("inbox"), indent=2))
    elif args.command == "transcript":
        print(json.dumps(client.request("transcript", conversation_id=args.conversation), indent=2))
    else:
        listener = Listener(client)
        print(json.dumps({"state": "listening", "thread_id": listener.thread_id, "resumed": listener.resumed}), flush=True)
        try:
            while True:
                receipt = listener.once()
                if receipt:
                    print(json.dumps(receipt), flush=True)
                if args.once:
                    break
                time.sleep(2 if receipt else 5)
        except KeyboardInterrupt:
            pass
        finally:
            listener.close()


if __name__ == "__main__":
    main()
