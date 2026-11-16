# Resume Builder dashboard

Extract this ZIP inside the existing `C:\Users\codet\AI_Resume_Builder_v2_Windows` folder. Keep the ZIP's `dashboard` directory intact. Double-click `dashboard\Start_Dashboard.bat` and keep its terminal open. The browser opens at [127.0.0.1:8765](http://127.0.0.1:8765).

Use **Queue candidate** for verified JSON, **Inbox** for job history and retry errors, **Resume outputs** for PDFs, LaTeX, audit files and Overleaf links, and **Logs** for recent watcher and monitor output. The dashboard refreshes every eight seconds. A waiting item may already be in progress because the watcher records the result after an attempt finishes.

The existing Windows scheduled watcher remains responsible for Ollama, local builds and Overleaf. The dashboard is local to the laptop and does not change the watcher or existing outputs. Queueing identical validated profiles through the dashboard reuses the inbox file. To retry an existing failed job, leave the file in the inbox so the watcher can apply its normal retry schedule.

Use **Import candidate** to open a résumé. JSON inputs queue directly. DOCX, searchable PDF, TXT and Markdown use local Ollama and open an editable draft side by side with its source text. Review and use **Confirm and queue**. Image formats require Tesseract OCR on PATH. Legacy DOC, ODT and RTF require LibreOffice. Scanned PDFs need OCR before import. The local Ollama model defaults to `qwen2.5:7b` and can be changed with `--model`.

To update an existing dashboard, close its terminal, extract this ZIP over the existing dashboard files, and relaunch it. If launched from Downloads with `--root`, replace the Downloads copy and use the same command.

## Open on iPhone with Tailscale

Use the same Tailscale account on the RTX laptop and iPhone. Keep the dashboard on its default local bind address. In Windows PowerShell, check `tailscale serve status` for an existing service on 8443. If the port is free, run:

```powershell
tailscale serve --bg --https=8443 8765
```

Use the exact HTTPS address it prints. If asked to enable HTTPS for your tailnet, use the official prompt only if you administer that tailnet. Restart the updated dashboard with the new options and substitute your URL and Tailscale login:

```powershell
py -3 "$env:USERPROFILE\Downloads\Resume_Builder_Dashboard\dashboard\resume_dashboard.py" --root "C:\Users\codet\AI_Resume_Builder_v2_Windows" --phone-url "https://rtx.example.ts.net:8443" --phone-user "your-tailscale-login@example.com"
```

With Tailscale connected on iPhone, open that same HTTPS address in Safari. The Windows laptop and dashboard process must stay running. The dashboard checks the Tailscale login on proxied requests. Use Serve for this dashboard. Do not use Funnel or a public reverse proxy.

## Open via resumeops.tech

1. In Cloudflare One, create a self-hosted Access application for `resumeops.tech`, all paths. Allow the specific emails that need access. Copy its **Application Audience (AUD) Tag** from the application details.
2. In the Windows dashboard terminal, stop the old dashboard with Ctrl+C, install the updated dashboard files, and run the following. Substitute the observed team name and AUD tag.

```powershell
py -3 -m pip install "PyJWT[crypto]>=2.10,<3"
py -3 "$env:USERPROFILE\Downloads\Resume_Builder_Dashboard\dashboard\resume_dashboard.py" --root "C:\Users\codet\AI_Resume_Builder_v2_Windows" --web-url "https://resumeops.tech" --access-team "<team-name>" --access-audience "<application-AUD>"
```

If the dashboard was installed inside the project, point the Python command to that copy instead. Keep the dashboard terminal open.

3. On the healthy `resumeops-rtx` tunnel add a Published application route for `resumeops.tech` with service `http://127.0.0.1:8765`. Enable **Protect with Access** for that service, using the same team and audience. Keep the default HTTP Host Header so the public hostname reaches the dashboard. If Cloudflare reports an existing DNS record, replace only the conflicting root Hostinger parking record for `resumeops.tech`; retain any unrelated records.
4. From a separate device, open `https://resumeops.tech` in a private window. Verify it requires sign-in, then sign in with an allowed email and confirm the dashboard loads. Also verify an unsigned API request receives no candidate data.

The dashboard verifies the Access signature, issuer, audience and expiration, and keeps its direct listener on the RTX laptop. See [Cloudflare’s self-hosted application guide](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/).
