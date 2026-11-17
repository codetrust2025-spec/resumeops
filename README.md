# ResumeOps

ResumeOps combines candidate entry, target roles, keyword coverage, and template preview with the existing Ollama, LaTeX, PDF, Overleaf, and watcher pipeline. Confirm facts at intake. The watcher handles generation, validation, publication, verification, and retries. Target keywords are scored against the supplied facts and never inserted as qualifications.

## Isolated staging on Windows

Use a separate checkout and empty private inbox and output directories. Install Python 3.12, MiKTeX or TeX Live with Latin Modern, and Poppler. Then run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe dashboard\resume_dashboard.py --root . --port 18769
```

In a separate console from that checkout, create an inbox if the dashboard has not created it yet. Use an authorized Overleaf Chrome profile for verification.

```powershell
.\.venv\Scripts\python.exe -m resume_bot watch --inbox inbox --output output --overleaf --browser-profile 'C:\path\to\authorized-profile'
```

Open http://127.0.0.1:18769 for the four-step builder. Resume Preview, LaTeX Code and ATS Analysis are available for completed revisions. Open /operations for document import, watcher health, job details, alerts and logs. ATS readiness is a ResumeOps rubric. The keyword percentage is reported separately. See [the Level 1 parity report](LEVEL1_PARITY.md).

Set optional environment values from `.env.example` in the service account environment. If `RESUMEOPS_MONGODB_URI` is set, ResumeOps upserts role keyword records and ATS score metadata after PDF verification. It stores the score and source hash in MongoDB without candidate contact fields or document contents. Connection failures remain visible as a blocked job and are retried. With no MongoDB URI, canonical records remain on local disk. Never configure the original external provider key.

The watcher retries transient Ollama, MongoDB, browser and build failures with backoff. Missing facts must be supplied or corrected at intake. An identical source and model reuse the previous revision and validated PDF. The Overleaf upload journal prevents a new submission when the previous outcome is ambiguous.

## Restarting the watcher after a deployment

The watcher keeps the code it loaded at startup. To load new `resume_bot` code without an administrator console, create an empty file named `.resume-bot-restart` in the inbox. The watcher finishes the job it is running, removes the file, and exits between jobs. `start_windows.ps1` restarts it about 30 seconds later with the code now on disk. To confirm which code is running, read `inbox/.resume-bot-health.json`: `code` lists the git blob id of every loaded `resume_bot/*.py` file, and `pid` and `started` identify the process. Each generated `result.json` records `ollama_runtime`, which is Ollama's `/api/ps` report of how much of the model sat in GPU memory.

## Tests and release gates

```powershell
$env:RESUME_BUILDER_REQUIRE_BROWSER='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe dashboard\check_dashboard.py .
.\.venv\Scripts\python.exe dashboard\check_cloudflare_access.py
.\.venv\Scripts\python.exe dashboard\check_builder.py
```

CI tests the application and builds a source-only staging ZIP. Production promotion is manual and requires explicit approval following security and real end-to-end checks. Validate every release on a separate loopback port first.

For a production cutover, preserve the existing private inbox, output, Chrome profile and Overleaf journal. Coordinate the watcher and dashboard upgrade to avoid sending targeted requests to an old worker. The dashboard supports a separate data root and code root with `--root` and `--code-root`. The supervisor accepts an optional `code_root` configuration field and forwards it via `RESUMEOPS_CODE_ROOT`. Run the upgraded watcher from the verified code checkout, pointed at the existing data paths. Keep the previous dashboard release and watcher launcher available, retain targeted jobs during rollback, and resume them when the upgraded watcher is restored. Verify the local homepage, assets, status API and origin JWT gate, followed by the authorized public surface. Keep the production origin on loopback and retain its Cloudflare Access policy.
