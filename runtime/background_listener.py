"""Opt-in project listener; no MCP host, local relay or development workers."""
import argparse
import json
import signal
import threading
import time

from claude_engine import ClaudeListener
from consumer_listener import ConsumerListener, shared_listener
from client import Client, Listener
from listener_support import ListenerLock
from relay import private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--engine", choices=("codex", "claude"), required=True)
    args = parser.parse_args()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    guard = ListenerLock(args.config).acquire()
    listener = None
    client = Client(args.config)
    state_file = client.path.parent / "background-status.json"
    try:
        listener = ClaudeListener(client) if args.engine == "claude" else Listener(client)
        listener = shared_listener(client,args.engine,listener)
        state = {"listener": "running", "engine": args.engine, "thread_id": listener.thread_id}
        private_json(state_file, state)
        print(json.dumps({"listener": "running", "engine": args.engine}), flush=True)
        heartbeat_at = time.monotonic() + 20
        while not stopped.is_set():
            if isinstance(listener,ConsumerListener):
                if time.monotonic() >= heartbeat_at:
                    client.request('heartbeat')
                    heartbeat_at = time.monotonic() + 20
                receipt = listener.once()
                state['thread_id']=listener.thread_id;private_json(state_file,state)
            else:
                receipt = listener.once()
            if receipt:
                state["latest_result"] = receipt
                private_json(state_file, state)
                print(json.dumps({'job_id':receipt['job_id'],'state':receipt['state']} if 'job_id' in receipt else {"received": receipt["received"], "delivery": receipt["delivery"]["status"]}), flush=True)
            stopped.wait(2 if receipt or isinstance(listener,ConsumerListener) else 5)
    except Exception as error:
        private_json(state_file, {"listener": "error", "error": str(error)})
        print("Project listener stopped with an error; inspect bridge_status.", flush=True)
        raise
    finally:
        if listener:
            listener.close()
        guard.close()
        if stopped.is_set():
            private_json(state_file, {"listener": "stopped"})


if __name__ == "__main__":
    main()
