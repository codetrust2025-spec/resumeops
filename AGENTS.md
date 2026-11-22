# ResumeOps: how an agent ships a change

For Claude Code and Codex. Production runs on the RTX laptop (CODETRUST, user `codet`) behind the Cloudflare tunnel
`resumeops-rtx`; https://resumeops.tech is protected by Cloudflare Access. **Only an agent running on this laptop can
deploy** (Codex Desktop or Claude Code desktop, opened on this repository). A cloud agent can open pull requests but
cannot reach the host: no SSH, and the dashboard listens on 127.0.0.1 only.

## Ship a change

1. Branch from `main`, change the code, and add a test that fails without the change.
2. Run the CI parts locally from **PowerShell** (Git Bash's `pdftotext` emits Latin-1 and breaks PDF tests), with the
   worker's Python, which has Playwright:

   ```powershell
   $py = 'C:\Users\codet\AI_Resume_Builder_v2_Windows\.venv\Scripts\python.exe'
   $env:PYTHONUTF8 = '1'; $env:RESUME_BUILDER_REQUIRE_BROWSER = '1'
   foreach ($part in 'unit-a', 'unit-b', 'browser', 'live') { & $py ci/run_checks.py $part }
   ```

3. Push, open a pull request, wait for the six GitHub checks, then squash-merge.
4. Only when `C:\Users\codet\ResumeOpsDashboard\deploy\pending` is empty (one release at a time), stage the merged
   commit by its **full** SHA and PR number:

   ```powershell
   Set-Location C:\Users\codet\ResumeOpsDashboard\deploy
   & $py stage.py <full 40-character sha of main> <pr number>
   ```

   The scheduled task "ResumeOps Auto Deploy" then waits for CI on that exact commit, takes and verifies a backup,
   cuts over, runs the smoke checks and the full acceptance (two live AI-to-PDF jobs), and rolls back on any failure.
   Follow `deploy\logs\<short sha>-<time>.log` until `RESULT: deployed`.
5. Verify the signed-in public site in the operator's browser (the pipeline holds no Access credentials, so its
   `authenticated_https_session` item is always BLOCKED by design).

## Rules

- Never delete or rewrite candidate data, restore a backup, start processing an application, or change Cloudflare
  Access or Turnstile settings without the operator's explicit approval.
- AI drafts name only the candidate's own technologies and claim no results; the Career Objective follows the
  approved role-wide template (`broad_objective_problems`).
- Write scripts that contain regular expressions to a file and run the file: passing them through a shell heredoc can
  turn `\b` into a backspace character.
