"""Owner-authorized capabilities and bounded text edits. No shell execution."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from folder_access import FolderPolicy, reset_session
from relay import BridgeError, private_json

EDIT_TOOLS = [
    {"name": "bridge_write_file", "description": "Create one new UTF-8 text file inside the owner's approved project folder. Existing files are refused. Never use this to change permissions.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path inside the approved folder, for example README.md. Never supply an absolute path."}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}},
    {"name": "bridge_edit_file", "description": "Replace one exact, unique text selection in an existing approved project file. Read the file first. Refuses ambiguous selections and blocked paths.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path inside the approved folder, for example README.md. Never supply an absolute path."}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}},
]

def options(config):
    value = config.get('capabilities', {})
    return {key: value.get(key) is True for key in ('edits', 'web_search')}

def refresh_capabilities(client):
    """Only the authenticated owner's web controls grant capabilities."""
    try:
        grant = client.request('permissions')
    except BridgeError as error:
        if error.status != 404:
            raise
        grant = {}
    new = {key: grant.get(key) is True for key in ('edits', 'web_search')}
    changed = options(client.config) != new
    if changed:
        reset_session(client.config)
    if changed or client.config.get('capabilities') != new:
        client.config['capabilities'] = new
        private_json(client.path, client.config)
    return changed

class FileEditor:
    """Every mutation checks fresh server permission before touching a file."""
    def __init__(self, client):
        self.client = client
        self.changes = []

    def grant(self):
        grant = self.client.request('permissions')
        if grant.get('edits') is not True or grant.get('folder_state') != 'picked':
            raise BridgeError('Folder edits are off. The folder owner must enable them in the website.')
        folder = self.client.config.get('folder')
        if not folder:
            raise BridgeError('No local project folder is granted.')
        policy = FolderPolicy(folder)
        if grant.get('folder_name') != policy.root.name:
            raise BridgeError('The folder grant changed. Wait for the listener to reconnect.')
        return policy

    def target(self, policy, value):
        if not isinstance(value, str) or not value or any(c in value for c in ('\\','\x00','\r','\n')):
            raise BridgeError('Use a relative project file path.')
        rel = Path(value)
        if rel.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')) or policy.denied(value):
            raise BridgeError('That edit path is outside the grant or blocked.')
        target = policy.root / rel
        if target.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf'):
            raise BridgeError('Text edit tools cannot replace media files.')
        for component in [target, *target.parents]:
            if component == policy.root:
                break
            if component.is_symlink():
                raise BridgeError('Edits through symbolic links are blocked.')
        policy.path(target.parent, directory=True)
        if target.exists():
            policy.path(target)
            if target.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf'):
                raise BridgeError('Text edit tools cannot replace media files.')
        return target

    def save(self, policy, target, content, original=None):
        if not isinstance(content, str) or '\x00' in content:
            raise BridgeError('Only UTF-8 text can be edited.')
        data = content.encode('utf-8')
        if len(data) > 1024 * 1024:
            raise BridgeError('Text edits are limited to 1 MiB.')
        # Recheck revocation and policy immediately before the atomic commit.
        current = self.grant()
        if current.fingerprint != policy.fingerprint:
            raise BridgeError('The folder policy changed. Restart the listener.')
        target = self.target(current, target.relative_to(policy.root).as_posix())
        if original is None and target.exists():
            raise BridgeError('File already exists. Read it and use bridge_edit_file.')
        if original is not None and target.read_bytes() != original:
            raise BridgeError('The file changed while preparing the edit. Read it again.')
        if original is not None:
            backup = self.client.path.parent / 'edit-backups' / hashlib.sha256(original).hexdigest()
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not backup.exists():
                backup.write_bytes(original)
                backup.chmod(0o600)
        handle, temporary = tempfile.mkstemp(prefix='.bridge-edit-', dir=target.parent)
        try:
            with os.fdopen(handle, 'wb') as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if original is None:
                # link is exclusive: another writer cannot be silently overwritten.
                os.link(temporary, target)
                os.unlink(temporary)
            else:
                if target.is_symlink() or target.read_bytes() != original:
                    raise BridgeError('The file changed before saving. Read it again.')
                os.chmod(temporary, target.stat().st_mode & 0o777)
                os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        receipt = {'path': target.relative_to(policy.root).as_posix(), 'action': 'created' if original is None else 'edited', 'sha256': hashlib.sha256(data).hexdigest()}
        self.changes.append(receipt)
        fd=os.open(str(self.client.path.parent/'edit-log.jsonl'),os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
        with os.fdopen(fd,'a') as log:
            log.write(json.dumps(receipt)+'\n')
        return receipt

    def run(self, name, args):
        policy = self.grant()
        target = self.target(policy, args.get('path'))
        if name == 'bridge_write_file':
            return self.save(policy, target, args.get('content'))
        if name != 'bridge_edit_file':
            raise BridgeError('Unknown project edit tool.')
        original = target.read_bytes()
        text = original.decode('utf-8')
        old, new = args.get('old_text'), args.get('new_text')
        if not isinstance(old, str) or not old or not isinstance(new, str) or text.count(old) != 1:
            raise BridgeError('The old text must match exactly one selection. Read the file again.')
        return self.save(policy, target, text.replace(old, new, 1), original)

def main():
    """The only MCP server permitted in an edit-enabled Claude listener."""
    import sys
    from client import Client
    from folder_tools import FolderReader, READ_TOOLS
    from image_content import mcp_content
    client = Client(sys.argv[1])
    editor = FileEditor(client)
    reader = FolderReader(client.config["folder"],client.path)
    definitions = READ_TOOLS + (EDIT_TOOLS if options(client.config)["edits"] else [])
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        if method == 'initialize':
            result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'), 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'context-bridge-files', 'version': '0.4.3'}}
        elif method == 'tools/list':
            result = {'tools': definitions}
        elif method == 'ping':
            result = {}
        elif method == 'tools/call':
            try:
                params = request['params']
                handler = reader if params['name'] in [t['name'] for t in READ_TOOLS] else editor
                value = handler.run(params['name'], params.get('arguments', {}))
                result = {'content': mcp_content(value), 'isError': False}
            except Exception as error:
                result = {'content': [{'type': 'text', 'text': str(error)}], 'isError': True}
        else:
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
            continue
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)

if __name__ == '__main__':
    main()
