
# Recruitment simulation application

This Flask application extends the existing interview, resume-parsing, authentication, and job-posting flows with application eligibility, configurable recruitment rounds, timed MCQ assessments, coding submissions, weighted scores, recruiter rankings, and printable reports.

## Run locally

From the repository root, in PowerShell:

```powershell
cd "3. Deployment"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m nltk.downloader stopwords wordnet
Copy-Item .env.example .env
python -m flask --app app create-admin
python app.py
```

`3. Deployment/requirements.txt` contains the pinned packages needed by the web app; the repository-root `requirements.txt` retains additional desktop audio and model-training tools.

Set a strong `SECRET_KEY` in `.env`. `python app.py` serves the application through Waitress; `HOST` and `PORT` can override its bind address and port. `GEMINI_API_KEY` is optional: interview prompts and answer scores use Gemini when available and an explicitly rule-based fallback when the key or compatible client library is unavailable. The fallback evaluates answer detail and question-word overlap; it cannot verify technical correctness. Audio transcription initializes the Whisper model only when transcription is requested and may download model data on first use. Open `http://127.0.0.1:5000`.

The default SQLite database is `interview.db` in this directory. Startup creates newly introduced tables and safely adds the recruitment columns to existing SQLite tables; it does not drop or replace existing records. `DATABASE_URL` can point to another SQLAlchemy database, but migrating an existing non-SQLite schema must be done separately.

### Deploy on Render

Create a **Python Web Service** from this repository and set its **Root Directory** to `3. Deployment`. Use:

- **Build command:** `pip install -r requirements.txt && python -m nltk.downloader stopwords wordnet`
- **Start command:** `python app.py`
- **Python runtime:** `runtime.txt` (Python 3.11.9, compatible with the pinned runtime packages)

Set `SECRET_KEY` to a generated secret in Render's environment settings and set `SESSION_COOKIE_SECURE=True`. Render provides `PORT`; the Waitress entry point binds to `0.0.0.0` by default and reads that port. `GEMINI_API_KEY` is optional; add it only when using Gemini, and keep it in Render's secret environment settings.

Render's local filesystem is ephemeral unless a persistent disk is attached. Render persistent disks require a paid web service. For a single-instance deployment with a persistent disk mounted at `/var/data`, set `DATABASE_URL=sqlite:////var/data/interview.db` and `UPLOAD_FOLDER=/var/data/uploads`. Without persistent storage, SQLite records and uploaded resumes may be lost on redeploy/restart. Do not use the sample local `sqlite:///interview.db` setting for persistent production data.

### Create the first administrator

Public registration creates candidate accounts only. From the `3. Deployment` directory, run `flask --app app create-admin` and follow the prompts to create an administrator account. The password is entered securely at the prompt; do not put real credentials in source code or commit them.

## Recruiter workflow

1. Sign in as a recruiter/admin and open **Jobs**.
2. Create a job, set eligibility rules, enable rounds, and ensure enabled round weights total 100%.
3. Open the job's aptitude/technical question banks to author and enable multiple-choice questions.
4. Open **Coding** to add problems, sample IO, and optional JSON test-case descriptions.
5. Review applications on the job leaderboard, set decisions, export CSV, or use **Print / Save PDF**.

Candidates can apply to jobs and use **My applications** to see eligibility, available rounds, results, overall weighted score, and a printable report. MCQ keys are evaluated server-side and are not sent to the candidate before submission.

Public registration creates candidate accounts only. Recruiter/admin accounts must be provisioned through the trusted `flask --app app create-admin` command; a request parameter cannot grant elevated roles.

## Scoring and safety notes

- Scores are stored on a 0-100 scale. The configured weighted average uses enabled rounds with submitted scores and renormalizes across the scores currently available; an uncompleted round is not silently treated as zero.
- Application statuses are `Applied`, `Under Review`, `Shortlisted`, `Selected`, and `Rejected`. Eligibility is displayed separately from recruiter-controlled decisions.
- Eligibility extraction is best-effort and uses resume text plus structured fields. CGPA/percentage comparisons use the same numeric scale as the job threshold; candidates with missing required information are marked ineligible when filtering is enabled.
- Skill equivalence uses a small transparent synonym map and keyword evidence; it is not an advanced semantic/ML model.
- Python/Java coding submissions are stored but **never executed** by this application. Recruiters must score them manually. Do not treat the optional test-case JSON as executed evaluation; production evaluation requires an isolated sandbox.
- When Gemini is configured, AI interview scoring requests ratings for technical/content quality, relevance, and completeness; it does not infer emotion, confidence, personality, or deception. Gemini-backed evaluation requires a compatible `google-generativeai` installation and valid API key; otherwise the disclosed rule-based fallback is used.
- All POST/PUT/PATCH/DELETE requests require a session-bound CSRF token. The legacy logout link is still a GET endpoint and should be converted to POST for strict method safety. Configure HTTPS and a strong secret for deployment.
