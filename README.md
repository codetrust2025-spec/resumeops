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

Open http://127.0.0.1:18769. The dashboard has eight sections: **Dashboard**, **Create Resume**, **Candidates**, **Resume Outputs**, **ATS Analysis**, **Templates**, **Activity Logs** and **Settings**. On narrow screens they sit behind the **Menu** button.

**Create Resume** is a five-step wizard:
1. **Personal details:** name, contact details, LinkedIn, GitHub, portfolio, headline and summary.
2. **Experience:** employment history, education, skills, projects and certifications. Years of experience and the Level 1 level label are derived from the dated roles you enter.
3. **Target role:** role, template, extra keywords, and keywords pulled from a pasted job description. Only known role/tool terms that appear in the text are used; nothing is scraped.
4. **Review & ATS:** source comparison, the Level 1 score breakdown (keywords 50, sections 30, content 20), priority keyword gaps, missing information, and an explicit fact confirmation.
5. **Generate:** queues the request and shows the watcher's real 0–100% progress, then the PDF preview, downloads and the Overleaf link.

The preview panel has **Resume preview**, **ATS analysis**, **LaTeX code** (copy or download `.tex`) and **PDF** tabs, plus validation errors. ResumeOps never generates experience, projects, metrics or qualifications. Every resume line comes from candidate-supplied or source-quoted facts. Imported documents still go through draft review, and **Open in resume wizard** loads a reviewed draft into the wizard. JSON intake, PDF links and the watcher dashboard work as before.

Candidate JSON accepts two optional Level 1 fields: `portfolio` (a string) and `projects` (a list of `{name, link, technologies[], bullets[]}`). Profiles without them normalize, hash and render byte-identically to earlier releases, so existing outputs and duplicate detection are unchanged.

Set optional environment values from `.env.example` in the service account environment. If `RESUMEOPS_MONGODB_URI` is set, ResumeOps upserts role keyword records and ATS score metadata after PDF verification. It stores the score and source hash in MongoDB without candidate contact fields or document contents. Connection failures remain visible as a blocked job and are retried. With no MongoDB URI, canonical records remain on local disk. Never configure the original external provider key.

The watcher retries transient Ollama, MongoDB, browser and build failures with backoff. Missing facts must be supplied or corrected at intake. An identical source and model reuse the previous revision and validated PDF. The Overleaf upload journal prevents a new submission when the previous outcome is ambiguous.

## Tests and release gates

```powershell
$env:RESUME_BUILDER_REQUIRE_BROWSER='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe dashboard\check_dashboard.py .
.\.venv\Scripts\python.exe dashboard\check_cloudflare_access.py
node --test tests/js/core.test.js
.\.venv\Scripts\python.exe dashboard\check_ui.py . .\ui-screenshots
```

`check_ui.py` is the browser end-to-end check. It runs in an isolated temporary workspace with the real dashboard and watcher pipeline, using real LaTeX, PDF compilation and verification. Ollama is replaced by a local echo server, and the Overleaf upload is stubbed. It drives all five wizard steps and checks every section at desktop, tablet and mobile widths for horizontal overflow and console errors, saving screenshots along the way.

CI tests the application and builds a source-only staging ZIP. Production promotion is manual and requires explicit approval following security and real end-to-end checks. Validate every release on a separate loopback port first.

For a production cutover, preserve the existing private inbox, output, Chrome profile and Overleaf journal. Coordinate the watcher and dashboard upgrade to avoid sending targeted requests to an old worker. The dashboard supports a separate data root and code root with `--root` and `--code-root`. The supervisor accepts an optional `code_root` configuration field and forwards it via `RESUMEOPS_CODE_ROOT`. Run the upgraded watcher from the verified code checkout, pointed at the existing data paths. Keep the previous dashboard release and watcher launcher available, retain targeted jobs during rollback, and resume them when the upgraded watcher is restored. Verify the local homepage, assets, status API and origin JWT gate, followed by the authorized public surface. Keep the production origin on loopback and retain its Cloudflare Access policy.
