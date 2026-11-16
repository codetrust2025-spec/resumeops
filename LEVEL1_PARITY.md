# Level 1 feature parity

Compared Bhavana’s archived React source and Express routes with the production baseline at 52a4890 and the parity branch. The downloaded archive is named resume-automation.zip. The archive contains the seven React components below, the backend routes, and role data. It contains no template directory or Git history.

## Frontend and behavior

| Original surface | Production baseline | Parity branch and verification |
|---|---|---|
| Header and app entry | Partial. Operations was the homepage. | Builder at /, Operations at /operations, and navigation to history. Browser-verified. |
| RoleSelector | Partial. Role dropdown inside one form. | Wizard role selection across the original catalog, role terms, and back navigation. The control is a dropdown. Browser-verified. |
| PersonalInfoForm | Partial. Contact fields existed, with no separate step or current company control. | Separate step with original fields plus portfolio. Name and valid email required. Phone remains optional for the existing verified intake. Current company is retained and prefills the work entry. Browser-verified. |
| ExperienceForm | Partial. Detailed work entries, without the original experience level control. | 0–25+ slider and the original four level thresholds, current-company prefill, repeatable work/education, projects and certifications. Slider is targeting metadata and does not fabricate experience. Browser-verified. |
| GenerateButton | Partial. Preview and queue appeared inline. | Dedicated review, validation, explicit confirmation, generation and actual watcher milestones with errors and retries. Browser-verified. |
| ResultsDisplay preview | Partial. Preflight text and separate artifact links. | Dedicated Resume Preview includes saved sections, inline PDF, verified Overleaf link when present, and download buttons. Browser-verified. |
| ResultsDisplay LaTeX | Partial. Artifact download only. | Dedicated source view, clipboard copy, and byte-verified download. Browser-verified. |
| ResultsDisplay ATS | Partial. Literal keyword coverage only. | Separate 100-point ResumeOps rubric with 50 role-keyword, 30 structure and 20 evidence points. Raw keyword coverage is separate. Matched/missing lists and evidence-safe suggestions are present. Browser-verified. |
| Generate Another | Missing. | Resets the wizard and preserves revision history. Browser-verified. |
| History and resume retrieval | Partial. Operations listed outputs, without reopening the builder’s results. | Search and reopen results from saved artifacts and ATS data. Legacy scores are interpreted read-only. Browser verified that reopening preserves output hashes. |
| Template choice | Partial. Production had two layouts, while Level 1 defaulted its template input with no visible selector. | Standard/Compact select and description, verified generated LaTeX and PDF. |
| Keywords management | Partial. Default list and request-specific additions. | Read, match catalog terms from a pasted description, save reusable role defaults, and add request-specific terms. Browser-verified with authenticated write requirement. |
| Document import and operations | Fully implemented in Level 2. | Operations retains import/draft review, workflow state, alerts, logs and artifact links. Existing integration and Access checks passed. |

## API comparison

| Original route | Baseline | Parity behavior |
|---|---|---|
| GET /api/health | Status API implemented. | GET /api/status reports health, jobs and revisions. |
| GET /api/roles and GET /api/roles/:roleId | Implemented in catalog. | GET /api/catalog returns original role IDs, names, terms and title variants; builder selects by ID. |
| POST /api/generate | Partial. Queue path existed. | Authenticated POST /api/preview validates and scores; POST /api/queue queues; GET /api/status reports actual completion. |
| GET /api/generate/download/:resumeId | Artifact download implemented. | Authenticated /api/artifact supports LaTeX attachments, inline PDF and PDF attachments. |
| GET /api/generate/:resumeId | Partial. Status and individual artifacts. | Authenticated GET /api/revision retrieves one saved profile and its analysis. |
| GET /api/generate/user/:userId | Partial. Operations output inventory. | Authenticated history and searching by candidate. The app retains existing Access-scoped shared history and does not expose an unauthenticated email lookup. |
| GET /api/keywords/:role | Partial. Static catalog and job-specific additions. | GET /api/keywords returns role terms and saved defaults. |
| POST /api/keywords/extract | Missing. | POST /api/keywords/extract matches terms against a pasted job description and returns the role catalog when the description is empty. The archived scraper used preset/mock data; live job-site scraping was never implemented. |
| POST /api/keywords/update | Missing. | POST /api/keywords/update persists validated, authenticated role defaults outside Git. |
| GET /api/templates and GET /api/templates/:templateId | Two renderer choices present. | GET /api/catalog lists both configured layouts. Result LaTeX is available through artifacts. The archive’s raw-template routes referenced a directory absent from the ZIP, so there are no legacy template files to restore. |

The original route names and Express response schemas were replaced by the existing authenticated ResumeOps API. The legacy provider, fabricated experience prompts, placeholder scraper source URLs, and unsupported hiring guarantees are excluded. No key or legacy database was used. Only explicit candidate facts reach the renderer; a lack of facts is resolved during validation or review.

## Evidence

The original screenshots run the archived React build against its original role catalog and an intercepted synthetic generation response. The restored screenshots use the automated parity browser test, with a deterministic edit fixture and real TeX/PDF generation. The staging test also ran a synthetic job with live Ollama, isolated MongoDB, authenticated Overleaf, and matched local/remote PDFs. Production deployment requires approval.
