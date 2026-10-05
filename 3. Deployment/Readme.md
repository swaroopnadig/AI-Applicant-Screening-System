
# Recruitment simulation application

This Flask application extends the existing interview, resume-parsing, authentication, and job-posting flows with application eligibility, configurable recruitment rounds, timed MCQ assessments, coding submissions, weighted scores, recruiter rankings, and printable reports.

## Run locally

From the repository root, in PowerShell:

```powershell
cd "3. Deployment"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r ..\requirements.txt
Copy-Item .env.example .env
python app.py
```

Set a strong `SECRET_KEY` in `.env`. `GEMINI_API_KEY` is only needed when using Gemini-powered interview responses and scoring; the application and non-AI pages start without it. Audio transcription initializes the Whisper model only when transcription is requested and may download model data on first use. Open `http://127.0.0.1:5000`.

The default SQLite database is `interview.db` in this directory. Startup creates newly introduced tables and safely adds the recruitment columns to existing SQLite tables; it does not drop or replace existing records. `DATABASE_URL` can point to another SQLAlchemy database, but migrating an existing non-SQLite schema must be done separately.

## Recruiter workflow

1. Sign in as a recruiter/admin and open **Jobs**.
2. Create a job, set eligibility rules, enable rounds, and ensure enabled round weights total 100%.
3. Open the job's aptitude/technical question banks to author and enable multiple-choice questions.
4. Open **Coding** to add problems, sample IO, and optional JSON test-case descriptions.
5. Review applications on the job leaderboard, set decisions, export CSV, or use **Print / Save PDF**.

Candidates can apply to jobs and use **My applications** to see eligibility, available rounds, results, overall weighted score, and a printable report. MCQ keys are evaluated server-side and are not sent to the candidate before submission.

Public registration creates candidate accounts only. Recruiter/admin accounts must be provisioned through a trusted administrator workflow; a request parameter cannot grant elevated roles.

## Scoring and safety notes

- Scores are stored on a 0-100 scale. The configured weighted average uses enabled rounds with submitted scores and renormalizes across the scores currently available; an uncompleted round is not silently treated as zero.
- Eligibility extraction is best-effort and uses resume text plus structured fields. CGPA/percentage comparisons use the same numeric scale as the job threshold; candidates with missing required information are marked ineligible when filtering is enabled.
- Skill equivalence uses a small transparent synonym map and keyword evidence; it is not an advanced semantic/ML model.
- Python/Java coding submissions are stored but **never executed** by this application. Recruiters must score them manually. Do not treat the optional test-case JSON as executed evaluation; production evaluation requires an isolated sandbox.
- AI interview scoring depends on the configured Gemini API and returns model-generated ratings; it does not infer emotion, confidence, personality, or deception.
- All POST/PUT/PATCH/DELETE requests require a session-bound CSRF token. The legacy logout link is still a GET endpoint and should be converted to POST for strict method safety. Configure HTTPS and a strong secret for deployment.
