"""Supervise the loopback dashboard, validate releases, and promote only healthy upgrades."""
import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HOME = Path(__file__).resolve().parent
CONTROL = HOME / 'release-state.json'
CONFIG = HOME / 'dashboard-config.json'
REQUIRED = ('index.html', 'dashboard.js', 'dashboard.css', 'core.js', 'workflow.js', 'resume_dashboard.py',
            'cloudflare_access.py', 'check_dashboard.py', 'check_cloudflare_access.py')
PORT = 8765
LOGS = HOME / 'logs'

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def write(path, value):
    fd, temporary = tempfile.mkstemp(dir=HOME, suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def release(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', name):
        raise ValueError('Invalid release name')
    folder = HOME / 'releases' / name
    missing = [file for file in REQUIRED
               if not (folder / file).is_file() or not (folder / file).stat().st_size]
    if missing:
        raise RuntimeError(f'{name} is missing required files: {", ".join(missing)}')
    return folder

def fetch(port, path, host=None):
    url = f'http://127.0.0.1:{port}{path}'
    with urlopen(Request(url, headers={'Host': host} if host else {}), timeout=5) as response:
        assert response.status == 200, f'{path}: HTTP {response.status}'
        return response.headers.get_content_type(), response.read()

def health(port, config):
    for route, content_type, marker in (
        ('/', 'text/html', b'id="candidate"'),
        ('/dashboard.js', 'text/javascript', b'function'),
        ('/core.js', 'text/javascript', b'ResumeCore'),
        ('/dashboard.css', 'text/css', b'{'),
    ):
        actual_type, body = fetch(port, route)
        if actual_type != content_type or marker not in body:
            raise RuntimeError(f'{route}: missing expected application content')
    mime, body = fetch(port, '/api/status')
    status = json.loads(body)
    if (mime != 'application/json' or not isinstance(status.get('healthy'), bool)
            or not isinstance(status.get('jobs'), list) or not isinstance(status.get('revisions'), list)):
        raise RuntimeError('/api/status: unexpected dashboard schema')
    try:
        fetch(port, '/', config['web_url'].removeprefix('https://'))
    except HTTPError as error:
        if error.code == 403:
            return
        raise
    raise RuntimeError('Public Host without JWT was accepted')

def spawn(name, port, config, output):
    folder = release(name)
    project = Path(config['project_root'])
    if not (project / 'resume_bot' / 'profile.py').is_file():
        raise RuntimeError('Configured project root is unavailable')
    env = os.environ.copy()
    if config.get('code_root'):
        env['RESUMEOPS_CODE_ROOT'] = config['code_root']
    env['PATH'] = os.pathsep.join([*config['tool_paths'], env['PATH']])
    return subprocess.Popen([
        sys.executable, '-u', str(folder / 'resume_dashboard.py'), '--root', str(project),
        '--port', str(port), '--model', config['model'], '--web-url', config['web_url'],
        '--access-team', config['team'], '--access-audience', config['audience'],
    ], cwd=folder, stdout=output, stderr=subprocess.STDOUT, env=env)

def stop(process):
    if process and process.poll() is None:
        if os.name == 'nt':
            # A Windows venv launcher can own a separate Python server process.
            subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

def listen(name, port, config, output):
    release(name)
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', port)) == 0:
            raise RuntimeError(f'Loopback port {port} is already occupied')
    process = spawn(name, port, config, output)
    deadline = time.monotonic() + 30
    last_error = None
    while process.poll() is None and time.monotonic() < deadline:
        try:
            health(port, config)
            logging.info('Healthy release=%s port=%s pid=%s', name, port, process.pid)
            return process
        except Exception as error:
            last_error = error
            time.sleep(1)
    stop(process)
    raise RuntimeError(f'Startup failed: {name}, exit={process.returncode}, probe={last_error}')

def service(port, config, output):
    process = None
    running = ''
    try:
        while True:
            state = read(CONTROL)
            requested = state.get('pending')
            selected = requested or state['active']
            if not process or process.poll() is not None or running != selected:
                if process and process.poll() is not None:
                    logging.warning('Dashboard exited with code %s; restarting', process.returncode)
                stop(process)
                process = None
                try:
                    process = listen(selected, port, config, output)
                except Exception:
                    logging.exception('Dashboard failed to start')
                    if requested:
                        write(CONTROL, {'active': state['active'], 'previous': state.get('previous')})
                        logging.warning('Rolled back failed upgrade to %s', state['active'])
                    elif state.get('previous') and state['previous'] != selected:
                        write(CONTROL, {'active': state['previous'], 'previous': None})
                        logging.warning('Rolled back failed active release to %s', state['previous'])
                    time.sleep(5)
                    continue
                running = selected
                if requested:
                    write(CONTROL, {'active': running, 'previous': state['active']})
                    logging.info('Promoted %s; rollback release=%s', running, state['active'])
            try:
                health(port, config)
            except Exception:
                logging.exception('Dashboard health failed; restarting')
                stop(process)
            time.sleep(5)
    finally:
        stop(process)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('run', 'check', 'verify', 'select', 'rollback'))
    parser.add_argument('release', nargs='?')
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    logging.basicConfig(filename=LOGS / 'supervisor.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
    config = read(CONFIG)
    if args.command == 'check':
        health(args.port, config)
        print('Home, JavaScript, CSS, status, and the origin JWT gate passed.')
    elif args.command in ('rollback', 'select'):
        state = read(CONTROL)
        name = args.release if args.command == 'select' else state.get('previous')
        if not name or name == state['active']:
            parser.error('No different target release is available')
        release(name)
        write(CONTROL, {**state, 'pending': name})
        print(f'Selected {name}; supervisor will promote after health checks.')
    else:
        with (LOGS / 'dashboard.log').open('ab', buffering=0) as output:
            if args.command == 'verify':
                if args.port == PORT:
                    parser.error('Choose an alternate loopback port for preflight')
                process = listen(args.release, args.port, config, output)
                try:
                    time.sleep(1)
                    health(args.port, config)
                    print(f'{args.release}: health checks passed on port {args.port}.')
                finally:
                    stop(process)
            else:
                service(args.port, config, output)

if __name__ == '__main__':
    try:
        main()
    except Exception:
        logging.exception('Supervisor failed')
        raise
