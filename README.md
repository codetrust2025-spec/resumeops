# ResumeOps

ResumeOps combines candidate entry, target roles, keyword coverage, and template preview with the existing Ollama, LaTeX, PDF, Overleaf, and watcher pipeline. Confirm facts at intake. The watcher handles generation, validation, publication, verification, and retries. Target keywords are scored against the supplied facts and never inserted as qualifications.

## Create Resume

**Create Resume** has two modes with one layout:

- **Upload Files** has only **Choose Files** (up to 10 PDF, DOCX, TXT, JPG or PNG files, 12 MB in all), **Target Role**, the required consent and **Generate Resume**. Every other fact comes from the documents. A fact they do not state becomes a question on the resume card; nothing is guessed.
- **Enter Manually** is a separate form for typing the candidate's details.

**Target Role** offers the catalog roles in `resume_bot/roles.json`, plus **Custom Role** for any other title. A title that is a catalog role name or one of its title variations uses that role. Any other title is kept as typed on the resume and takes its ATS keywords from the nearest catalog role, so no title is left without keywords. For example, QA Engineer takes the Software Tester keywords, Senior Software Engineer the Software Engineer ones and Big Data Engineer the Data Engineer ones (`nearest_role` in `resume_bot/ai_review.py`).

**My Resumes** shows three metrics (Total Resumes, Completed, In Progress). Each resume card shows its own live progress, with the AI verification details collapsed.

## AI Edit

AI Edit changes a finished resume. Both entry points use one engine (`resume_bot/ai_edit.py`):

- **AI Edit on a resume card** works on that exact resume, with no candidate selection.
- **AI Edit in the My Resumes header** (global) finds the resume from the request: the candidate by name, then the resume by the role the request names. When more than one resume matches, it lists them to choose from.

A request can be a correction in plain words ("Change Bhavana's graduation year from 2024 to 2026", "Add Docker to the skills", "Remove Git from skills"), a new target role ("Regenerate as DevOps Engineer"), or a job description. A job description can be pasted or uploaded as a PDF, DOCX, TXT, JPG, JPEG or PNG file of up to 6 MB. AI Edit then lists the job keywords the verified facts support, the skill gaps it will not add, and the Career Objective priorities. It reorders skills, work lines and projects so that what the job asks for comes first. It uses verified candidate facts only, and never adds a skill, technology or result that the facts do not state.

**Original vs edited, before approval.** The preview lists every proposed change in a table with Apply, Change, Original and Edited columns. Each change has its own tick box. Untick any change you do not want, then choose **Apply**. Nothing changes until you apply.

Applying creates a new version. It regenerates the PDF and LaTeX, recalculates the ATS score and syncs Overleaf. The original resume stays unchanged.

**Compare with original.** A resume made by AI Edit has a **Compare with original** button on its card. It shows every field that differs between that version and the resume it came from.

## Isolated staging on Windows

The automation intake accepts candidate evidence documents, including education records, earned
certificates, experience letters, project descriptions and resumes. Users can submit up to 10 PDF,
DOCX, TXT, JPG or PNG documents for one candidate, with a combined 12 MiB limit. The server validates
the same limits, reads every document and combines the evidence before asking for missing facts.
Text-based PDFs are read with Poppler. Image-only and mixed scanned PDFs are rendered with
`pdftoppm` and OCR is limited to 20 pages. JPG/PNG and rendered pages use Tesseract when installed,
or Windows' built-in OCR on Windows. Linux installations need `tesseract-ocr`; Windows needs its OCR
language feature. The Windows helper runs with process-scoped `RemoteSigned`, honoring enforced
Group Policy without changing persistent execution policy. OCR failure asks the user for clearer
evidence or manual entry. Include `resume_bot/ocr_windows.ps1` when packaging Windows code.
Legacy single-document submissions retain their request format and continue to work.

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

## Ollama model selection

Operators can open **Candidates → Model settings** to choose an installed local Ollama model or restore the
service default. The choice is shared by the dashboard and watcher and survives restarts. New AI work picks it
up without a watcher restart. Work already in progress keeps its model, and changing the choice does not
regenerate published resumes. The dashboard and watcher should share the same data root and Ollama server
(`OLLAMA_URL`, normally `http://127.0.0.1:11434`). Each service uses its `--model` or `OLLAMA_MODEL` default
when no override is saved.

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

### Single-submit release

`/automated` accepts a supported resume file or basic candidate details, a target title, and publication consent. The dashboard persists the submission and prepares a source-backed profile. The watcher renders and verifies it using its existing Overleaf session. The model ranks verified facts within their original sections. The target headline expresses an objective, and historical job titles stay as supplied.

For production, set `automation_first` to `true` in the supervisor configuration only after the watcher and dashboard use the same source revision. Populate `operator_accounts` with authorized Cloudflare email identities for the shared legacy History and Operations actions. Automated History and downloads belong only to the submitting account. Private automation work uses `inbox-private`, `output-private` and `.resumeops-automation.sqlite3` alongside the existing data paths. Earlier releases do not scan those directories. Keep them and their approval records during rollback so the matching release can resume work.

Run `python dashboard/check_automation.py` from the source root. It exercises the real local PDF compiler and a fixture Overleaf adapter, along with mobile one-submit, account isolation, retry and deduplication. Run a separate authenticated HTTPS acceptance test with the authorized Overleaf account after cutover.
