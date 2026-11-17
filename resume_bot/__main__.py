import argparse
import json
from pathlib import Path
from .pipeline import process, scan
from .overleaf import login_setup


def main():
    p=argparse.ArgumentParser(prog='python -m resume_bot',description='Local Ollama Resume -> LaTeX -> PDF -> Overleaf automation')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('run','watch'):
        subp=sub.add_parser(name)
        subp.add_argument('--input' if name=='run' else '--inbox',required=True)
        subp.add_argument('--output',default='output')
        subp.add_argument('--model',default='qwen2.5:7b')
        subp.add_argument('--ollama-url',default='http://127.0.0.1:11434')
        subp.add_argument('--no-polish',action='store_true',help='Only for offline debugging; AI normally mandatory')
        subp.add_argument('--overleaf',action='store_true',help='Import final ZIP using signed-in browser profile')
        subp.add_argument('--browser-profile',default='.overleaf-browser-profile')
        subp.add_argument('--max-pages',type=int,default=2)
        if name=='watch':
            subp.add_argument('--once',action='store_true')
            subp.add_argument('--interval',type=int,default=30)
    doctor=sub.add_parser('doctor',help='Check local dependencies and current Ollama connectivity')
    doctor.add_argument('--ollama-url',default='http://127.0.0.1:11434')
    doctor.add_argument('--model',default='qwen2.5:7b')
    sign=sub.add_parser('overleaf-login',help='One-time owner sign-in on local browser')
    sign.add_argument('--browser-profile',default='.overleaf-browser-profile')
    a=p.parse_args()
    if a.command=='doctor':
        import shutil
        import urllib.request
        checks={tool:bool(shutil.which(tool)) for tool in ('pdflatex','pdfinfo','pdftotext')}
        try:
            import playwright
            checks['playwright_installed']=True
        except ImportError:
            checks['playwright_installed']=False
        try:
            with urllib.request.urlopen(a.ollama_url.rstrip('/')+'/api/tags',timeout=3) as response:
                checks['ollama_models']=json.load(response).get('models',[])
                checks['ollama_connected']=True
                checks['requested_model']=a.model
                checks['model_installed']=any(x.get('name')==a.model or x.get('model')==a.model for x in checks['ollama_models'])
        except Exception as exc:
            checks['ollama_connected']=False
            checks['ollama_error']=str(exc)
        print(json.dumps(checks,indent=2))
        return
    if a.command=='overleaf-login':
        login_setup(a.browser_profile)
        return
    from .ollama import Ollama
    ai=Ollama(a.model,a.ollama_url)
    if a.command=='run':
        result=process(a.input,a.output,ai=ai,polish_enabled=not a.no_polish,
                       overleaf=a.overleaf,browser_profile=a.browser_profile,max_pages=a.max_pages)
        print(json.dumps(result,indent=2))
    else:
        watch(a, ai)


def watch(a, ai, sleep=None):
    """Run the watcher. If another watcher already holds the inbox lock (for example the boot task
    and a logon task both started one), stand by and take over when it stops, instead of crashing."""
    import sqlite3
    import time
    sleep = sleep or time.sleep
    waiting = False
    while True:
        try:
            return scan(a.inbox,a.output,ai=ai,polish_enabled=not a.no_polish,
                        overleaf=a.overleaf,browser_profile=a.browser_profile,
                        once=a.once,interval=a.interval,max_pages=a.max_pages)
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or a.once:
                raise
            if not waiting:
                print('Another watcher is active for this inbox; standing by', flush=True)
                waiting = True
            sleep(30)


if __name__=='__main__':
    main()
