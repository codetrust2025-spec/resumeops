# ResumeOps: AI-first resume builder (implementation contract)

Status: **integration branch, pending release verification**. The dashboard serves the
AI-first intake at its root and uses the existing workflow view for progress, verified
outputs and history. This branch incorporates PR #17 for testing; review and rollout
of that dependency remain independent release gates.


## Candidate input — exactly fourteen categories

1. Target role or job title (`role`).
2. Total years of experience (`years_of_experience`).
3. Full name (`full_name`).
4. Email (`email`).
5. Phone (`phone`).
6. Location (`location`).
7. Current company (`current_company`).
8. LinkedIn (`linkedin`).
9. GitHub (`github`).
10. Portfolio URL (`portfolio`).
11. Work experience (multiple; employer, designation, start/end dates and location only) (`work_experience`).
12. Education (multiple; degree, specialization, university, graduation year, CGPA) (`education`).
13. Projects (multiple; titles/names; source-imported notes may be retained as evidence internally) (`projects`).
14. Coding platforms (optional; platform name and URL) (`coding_platforms`).

No candidate-written summary, objective, skills, achievement bullets, work duties, certification list, target keyword list, or pasted job description should be required or requested as an extra form input. AI can use extracted original resume text as internal source evidence; do not treat past resume statements as employer-verified facts.

## Proposed end-to-end flow

1. **Start** → choose import (PDF/DOCX/TXT etc.) or fill the fourteen intake categories.
2. **Extract / prefill** → preserve only candidate-provided basic details in the input form; preserve original document and attributable evidence separately; do not silently use an inferred skill as confirmed.
3. **Role intelligence** → combine role catalog with authenticated, publicly accessible job listings when lawful and available. Record source URL, role, retrieval time, and keyword evidence. Never label locally curated keywords as “current Naukri trends” without fresh sources. On failed fetch, continue using clearly labeled role-catalog suggestions.
4. **AI drafting** → Ollama produces professional summary, technical skills, core competencies, role- and years-appropriate work bullet proposals, candidate-project-description proposals, 2–3 suggested project **ideas** when real project details are absent, proposed achievement wording, and training/certification recommendations. Candidate must never write these sections manually as a prerequisite.
5. **Suggestion review** → each generated claim is shown beside its provenance: **verified from supplied source**, **candidate-confirmed**, **unverified suggestion**, **portfolio idea (not built)**, or **learning goal (not earned)**. Candidate can accept, edit or reject. Approval is an attestation to facts actually true for them; accepting a suggestion cannot convert a hypothetical project into completed work.
6. **Safety gate** → final resume may include only supplied, evidenced, or expressly candidate-confirmed factual claims. Unverified technologies, skills, responsibilities, metrics, tenure, certifications and generated project ideas must be excluded. A hypothetical role-based project can be presented separately as a future learning project, never as a completed work project.
7. **ATS/Naukri optimization** → select keywords from verified candidate capabilities and role vocabulary, use natural language without stuffing, show missing but relevant capabilities as suggestions rather than adding false claims. Distinguish recruiter-search matching from factual experience.
8. **Generation** → normal validated queue → RTX watcher → Ollama constrained improvements → ATS → deterministic LaTeX/PDF → Overleaf → verification → downloads/history/auto-reset. The approved, evidence-labeled profile must be immutable at submission and auditable by job ID.
9. **Recovery** → form, imported file, AI suggestions and approvals survive Cloudflare reauthentication; replay uses the same operation ID. Integrate with PR #17 after it is approved. Never lose in-flight work or create duplicate generation jobs.

## Experience-level composition

| Experience | Content policy |
| --- | --- |
| 0 years | Entry-level summary, education-first, **project ideas** clearly not completed, no invented internships |
| 1–3 years | Suggested implementation, debugging, API and testing bullets only where candidate confirms doing them |
| 3–5 years | Module ownership and integrations only with source evidence or candidate confirmation |
| 5–8 years | Architecture, mentoring, performance work only if genuinely supported |
| 8+ years | Leadership, system design and strategic contributions only if verified or candidate-confirmed |

Do not combine React + Angular, AWS + Azure, databases, or other incompatible stacks in a single project without candidate-supplied evidence or explicit confirmation. Separate work responsibilities from project implementation. Only use metrics supplied in evidence or confirmed from actual measurements; never invent impact percentages.

## Implementation scope

- Exactly fourteen intake categories on the AI-first page, with PDF, DOCX and TXT
  prefill. Import evidence stays in the authenticated server-side draft.
- Ollama proposals are opt-in, editable and scoped to their source experience or
  supplied project. Portfolio ideas and learning recommendations cannot enter the
  queue. Selected claims and the final normalized payload are enforced server-side.
- Each immutable approval queues through the existing watcher. Replaying the same
  approval returns the same job; attempting to change an approved draft returns 409.
- Role keywords are attributed to the curated ResumeOps catalog. External keyword
  attribution requires terms from an authorized source with its URL and retrieval date.
- The dashboard recovers approved work through the PR #17 Access flow.

## Release gates

1. Unit, contract, security, browser, API and integration CI pass.
2. PR #17 is reviewed and merged or its auth changes are deliberately integrated without conflict.
3. Reviewer approves the AI-first implementation; the current draft contract PR alone is **not sufficient**.
4. Backup existing candidates/history; verify idle imports; use supervisor rollback.
5. Test at least three synthetic profiles: fresher, 2–3 years, 8+ years, plus real imported documents through Cloudflare Access.
6. Verify only confirmed claims are in PDFs and that final generated content is tailored, useful and not fabricated. Report remaining Naukri-source and real-iPhone gaps honestly.
