"""Supervise one existing owner-authorized named connection with macOS launchd."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent

def definition(config, engine):
    config = Path(config).expanduser().resolve()
    data = json.loads(config.read_text())
    if not data.get('agent_id', '').startswith('agent:') or not data.get('url', data.get('relay_url', '')).endswith('/api/agents'):
        raise ValueError('Use an existing exact named-agent connection.')
    script = ROOT / ('named_' + engine + '_listener.py')
    installed=config.with_suffix('.runtime')/'worker-code'/script.name
    if installed.is_file():script=installed
    if engine not in ('codex', 'claude') or not script.is_file():
        raise ValueError('Choose the existing Codex or Claude runtime.')
    label = 'ai.context-bridge.named.' + hashlib.sha256(str(config).encode()).hexdigest()[:20]
    runtime = config.with_suffix('.runtime')
    return label, {
        'Label': label,
        'ProgramArguments': [sys.executable, str(script), '--config', str(config)],
        'WorkingDirectory': str(script.parent),
        'RunAtLoad': True,
        # Exit 0 means revoked or intentionally stopped. Crashes restart automatically.
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 30,
        'ProcessType': 'Background',
        'StandardOutPath': str(runtime / 'supervisor.log'),
        'StandardErrorPath': str(runtime / 'supervisor-error.log'),
        'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin')},
    }

def install(config, engine, replace_idle_pid=None):
    os.umask(0o077)
    config = Path(config).expanduser().resolve()
    label, spec = definition(config, engine)
    runtime = config.with_suffix('.runtime')
    runtime.mkdir(exist_ok=True, mode=0o700)
    domain = 'gui/' + str(os.getuid())
    target = domain + '/' + label
    # Never create another supervisor for an already registered exact connection.
    registered = subprocess.run(['launchctl', 'print', target], capture_output=True).returncode == 0
    if replace_idle_pid:
        state = json.loads((runtime / 'status.json').read_text())
        stamp = state.get('last_poll', state.get('checked_at', 0))
        if registered or state.get('pid') != replace_idle_pid or state.get('current_job_id') or state.get('state') not in ('running', 'listening') or time.time()-stamp > 25:
            raise RuntimeError('The previous exact listener must be idle and recently checked in.')
        argv = subprocess.check_output(['ps', '-p', str(replace_idle_pid), '-o', 'args='], text=True).strip()
        canonical = str(ROOT / ('named_' + engine + '_listener.py')) + ' --config ' + str(config)
        legacy = str(runtime / 'listener.py')
        if canonical not in argv and legacy not in argv:
            raise RuntimeError('Previous process does not match this connection.')
        if engine == 'claude' and subprocess.run(['pgrep', '-P', str(replace_idle_pid)], capture_output=True).returncode == 0:
            raise RuntimeError('Claude is finishing a reply; wait for its child process to finish.')
        os.kill(replace_idle_pid, signal.SIGTERM)
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            if subprocess.run(['ps', '-p', str(replace_idle_pid)], capture_output=True).returncode:
                break
            time.sleep(.2)
        else:
            raise RuntimeError('Previous listener has not stopped; no duplicate was started.')
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and plistlib.loads(path.read_bytes()).get('ProgramArguments') != spec['ProgramArguments']:
        raise RuntimeError('Existing service points elsewhere; preserve it.')
    path.write_bytes(plistlib.dumps(spec))
    path.chmod(0o600)
    if registered:
        subprocess.run(['launchctl', 'bootout', target], check=True, capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True)
    return {'label': label, 'engine': engine, 'installed': True, 'config_unchanged': True}

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    p.add_argument('--engine', required=True, choices=['codex', 'claude'])
    p.add_argument('--replace-idle-pid', type=int)
    args = p.parse_args()
    print(json.dumps(install(args.config, args.engine, args.replace_idle_pid)))

if __name__ == '__main__':
    main()
