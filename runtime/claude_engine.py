"""Claude Code engine: the Claude-side twin of client.Listener.

Answers bridge messages in one persistent headless `claude -p` session using the
owner's own Claude Code login. By default it is isolated: no tools, no CLAUDE.md,
memory, settings or git context (safe mode, replaced system prompt, neutral
folder), so it knows only the owner's project brief, like the Codex liaison.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from codex_rpc import LIAISON_INSTRUCTIONS
from relay import private_json
from folder_access import FolderPolicy, reset_session
from folder_tools import READ_TOOLS
from capabilities import options

PRIVATE_ENVIRONMENT = """Your context also shows this computer's account email, user name, folders
and system details. Never share any of them with the collaborator."""
SCHEMA = json.dumps({"type": "object", "properties": {"message": {"type": "string"}, "needs_reply": {"type": "boolean"}, "image_request":{"type":["string","null"]}},
                     "required": ["message", "needs_reply", "image_request"], "additionalProperties": False})


def find_claude(executable=None):
    """Find the owner's Claude Code CLI, including installs a GUI PATH misses."""
    home = Path.home()
    for candidate in (executable, shutil.which("claude"), home / ".local/bin/claude", home / ".claude/local/claude",
                      "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
    return None


class ClaudeListener:
    def __init__(self, client, tools=(), deny=(), instructions=LIAISON_INSTRUCTIONS, workspace=None, isolated=True):
        self.client = client
        self.isolated = isolated
        config = client.config
        self.capabilities=options(config)
        self.policy=FolderPolicy(config['folder']) if config.get('folder') else None
        fingerprint=self.policy.fingerprint if self.policy else 'brief'
        if config.get('folder_policy')!=fingerprint:
            reset_session(config);config['folder_policy']=fingerprint;private_json(client.path,config)
        self.binary = find_claude(config.get("claude_bin"))
        if not self.binary:
            raise RuntimeError("Claude Code CLI is required on this computer.")
        self.tools, self.deny, self.instructions = list(tools), list(deny), instructions
        if self.policy:
            self.tools=[]
            self.instructions=LIAISON_INSTRUCTIONS.replace('execute commands, access files, use tools,','use unapproved tools,')+'\n'+self.policy.instructions(self.capabilities)
        if self.capabilities['web_search']:
            self.tools.append('WebSearch')
            self.instructions+='\nWebSearch is owner-approved for public facts. Cite the source URLs and distinguish observed facts from inference.'
        if workspace is None and isolated:
            # A folder outside any repository, stable per connection so the session can resume.
            key = hashlib.sha256((config["project_id"] + config["identity"]).encode()).hexdigest()[:16]
            workspace = Path.home() / ".context-bridge" / "claude" / key
        self.workspace = Path(workspace or config["cwd"]).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        if config.get("claude_workspace") != str(self.workspace):
            # Sessions are stored per folder; a new folder needs a new session and brief.
            for stale in ("claude_session_id", "claude_session_started", "context_loaded"):
                config.pop(stale, None)
            config["claude_workspace"] = str(self.workspace)
            private_json(client.path, config)
        self.resumed = bool(config.get("claude_session_started"))
        if not config.get("claude_session_id"):
            config["claude_session_id"] = str(uuid.uuid4())
            private_json(client.path, config)
        self.thread_id = config["claude_session_id"]
        self.model = "claude"
        context_path = config.get("context_file")
        if context_path and not config.get("context_loaded"):
            self.ask("Owner supplied project context (retain for later messages):\n" + Path(context_path).read_text()
                     + "\nAcknowledge briefly. Set needs_reply=false. Do not forward this context wholesale.")
            config["context_loaded"] = True
            private_json(client.path, config)
        self.cache = client.path.parent / (config["identity"] + "-responses") / fingerprint
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)

    def ask(self, prompt):
        try:
            return self._ask(prompt)
        except Exception:
            # Any failed turn may have created or tainted a CLI session. Discard
            # it in memory and on disk so this same listener can recover.
            reset_session(self.client.config)
            private_json(self.client.path, self.client.config)
            raise

    def _ask(self, prompt):
        read_audit=self.client.path.parent/'read-log.jsonl'
        read_offset=read_audit.stat().st_size if read_audit.exists() else 0
        audit=self.client.path.parent/'edit-log.jsonl'
        audit_offset=audit.stat().st_size if audit.exists() else 0
        if self.policy and FolderPolicy(self.policy.root).fingerprint!=self.policy.fingerprint:
            raise RuntimeError('Folder policy changed; restart the listener for a fresh session.')
        if not self.client.config.get('claude_session_id'):
            self.client.config['claude_session_id'] = str(uuid.uuid4())
            private_json(self.client.path, self.client.config)
        self.thread_id = self.client.config['claude_session_id']
        started = self.client.config.get("claude_session_started")
        command = [self.binary, "-p", prompt, "--output-format", "json", "--json-schema", SCHEMA,
                   "--strict-mcp-config", "--tools", ",".join(self.tools)]
        file_tools=[]
        if self.policy:
            import sys
            file_tools=['mcp__project_files__'+tool['name'] for tool in READ_TOOLS]
            if self.capabilities['edits']:file_tools+=['mcp__project_files__bridge_write_file','mcp__project_files__bridge_edit_file']
            server={'mcpServers':{'project_files':{'command':sys.executable,'args':[str(Path(__file__).with_name('capabilities.py')),str(self.client.path)]}}}
            command += ['--mcp-config',json.dumps(server)]
        if self.isolated:
            command += ["--system-prompt", self.instructions + "\n" + PRIVATE_ENVIRONMENT,
                        "--setting-sources", ""]
            if file_tools:
                command += ['--disable-slash-commands','--settings',json.dumps({'disableAllHooks':True,'autoMemoryEnabled':False,'claudeMdExcludes':['**'],'enabledPlugins':{}})]
            else:command += ['--safe-mode']
        else:
            command += ["--append-system-prompt", self.instructions]
        if self.tools or file_tools:
            command += ["--allowedTools", ",".join(self.tools+file_tools)]
        deny=list(self.deny)
        if self.policy:
            deny+=['Read','Grep','Glob','Bash','Write','Edit']
            command += ['--restricted','--permission-mode','dontAsk','--permission-prompts','none','--add-dir',str(self.policy.root)]
            command[command.index('json')]='stream-json';command+=['--verbose']
        if deny:command += ["--disallowedTools"] + deny
        command += ["--resume" if started else "--session-id", self.thread_id]
        # DEVNULL: `claude -p` reads piped stdin, which here is the MCP host's request channel.
        environment=dict(os.environ)
        if self.isolated:
            environment.update(CLAUDE_CODE_DISABLE_CLAUDE_MDS='1',CLAUDE_CODE_DISABLE_AUTO_MEMORY='1',CLAUDE_CODE_DISABLE_ATTACHMENTS='1')
        result = subprocess.run(command, cwd=self.workspace, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600,env=environment)
        try:
            if self.policy:
                events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                payload=next((event for event in reversed(events) if event.get('type')=='result'),{})
                sources=[];unsafe=False;used_web_search=False
                for event in events:
                    message = event.get('message')
                    if not isinstance(message, dict):
                        continue
                    content = message.get('content')
                    if not isinstance(content, list):
                        continue
                    for item in content:
                        if not isinstance(item, dict):continue
                        if item.get('type')!='tool_use':continue
                        name=item.get('name');args=item.get('input',{})
                        try:
                            if name=='WebSearch' and self.capabilities['web_search']:used_web_search=True
                            elif name in file_tools:
                                # All reads and edits use the first-party server's
                                # fresh boundary checks; no builtin recursive tools.
                                pass
                            elif name not in ('StructuredOutput','EndConversation'):unsafe=True
                        except Exception:unsafe=True
                if unsafe or FolderPolicy(self.policy.root).fingerprint!=self.policy.fingerprint:
                    raise RuntimeError('Reply blocked and session discarded: tool or folder policy violation.')
            else:payload = json.loads(result.stdout);sources=[];used_web_search=False
        except ValueError:
            raise RuntimeError("Claude turn failed: no JSON result.")
        output = payload.get("structured_output")
        if payload.get("is_error") or not isinstance(output, dict) or not isinstance(output.get("message"), str) \
                or type(output.get("needs_reply")) is not bool:
            raise RuntimeError("Claude turn failed: " + str(payload.get("subtype")))
        if not started:
            self.client.config["claude_session_started"] = True
            private_json(self.client.path, self.client.config)
        if read_audit.exists():
            with read_audit.open() as log:
                log.seek(read_offset)
                for line in log:
                    for relative in json.loads(line).get('paths',[]):
                        self.policy.path(relative);sources.append(relative)
        if sources:output['message']+='\nRead: '+', '.join(sorted(set(sources))[:40])
        output['used_web_search']=used_web_search
        if audit.exists():
            with audit.open() as log:
                log.seek(audit_offset)
                changes=[json.loads(line) for line in log if line.strip()]
            if changes:output['changes']=changes
        return output

    def once(self):
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
            response = self.ask(prompt)
            private_json(cache_file, response)
        result = self.client.request("complete", message_id=message["id"], lease=message["lease"],
                                     text=response["message"], needs_reply=response["needs_reply"],
                                     idempotency_key=message["id"] + ":" + message["lease"])
        return {"identity": self.client.config["identity"], "thread_id": self.thread_id, "received": message["id"],
                "kind": message["kind"], "response": response, "delivery": result}

    def close(self):
        pass
