# AI Applicant Screening System — Project Functionality Report

## 1. Project overview

This web application supports job posting, candidate applications, recruitment screening, and interview practice in one platform. Candidates can apply and complete screening rounds; recruiters can review results, rank applicants, and update application decisions.

**Main purpose:** Make candidate screening and interview assessment easier to manage.

## 2. User roles

### Candidate

- Register for a candidate account and log in.
- Browse job postings and apply for a job.
- Upload or update a resume from the signed-in home page; view parsed profile fields, application eligibility, progress, available rounds, scores, and reports.
- Take enabled aptitude and technical multiple-choice assessments.
- Submit a Python or Java solution when a coding round is enabled.
- Start an AI interview round when enabled.
- Resume uploads support PDF, DOCX, and TXT.

### Admin / Recruiter

- Log in to recruiter/admin pages. Public registration creates candidate accounts only.
- The first administrator is created from the trusted command line with `flask --app app create-admin`.
- Create, edit, and delete jobs; set job requirements and enable screening rounds.
- Configure round weights and time limits.
- Create and manage aptitude/technical questions and coding problems.
- Review applications, eligibility, scores, coding submissions, and job rankings.
- Update application statuses, export applicant CSV data, and print/save reports.

## 3. Actual process flow

**Candidate registers and logs in** → Browses jobs and applies → System checks configured eligibility criteria and assigns Applied, Under Review, or Rejected → Candidate completes enabled assessment, coding, and/or interview rounds → System calculates available scores and job ranking → Recruiter reviews results and updates the application decision/status

Resume upload is available after candidate login. Eligibility depends on the information present in the candidate's resume data. The final hiring decision is made by the recruiter; the system does not automatically select or hire candidates.

## 4. Main modules

- **Authentication and roles** — Login, candidate registration, and access restrictions for candidate and recruiter/admin functions.
- **Job management** — Job postings, eligibility requirements, round settings, weights, and assessment time limits.
- **Resume parsing** — Extracts text and basic fields such as contact details, skills, education, projects, certifications, and CGPA/percentage where detectable.
- **Eligibility checking** — Checks required skills and configured experience, education, and CGPA/percentage requirements against parsed resume data.
- **Aptitude and technical assessments** — Recruiter-authored multiple-choice questions, timed candidate attempts, and automatic scoring after submission.
- **Coding round** — Recruiter-authored problems and sample inputs/outputs; candidates submit Python or Java code for manual review.
- **AI interview** — Interview questions and answer ratings are generated through the configured Gemini integration; audio transcription is provided through Faster-Whisper when available.
- **Scoring and ranking** — Stores round scores, calculates a weighted application score from scored rounds, and orders applications by score for a job.
- **Dashboards and reports** — Recruiter metrics, candidate application tracking, job leaderboard, printable application report, and CSV export.

## 5. Scoring

- **Resume score:** Based on how many of the job's required skills are found in the parsed resume text/skills list. Experience, education, and CGPA/percentage are eligibility checks; they are not separate components of the current final score. A missing resume remains unscored rather than counting as zero.
- **Aptitude and technical:** Correct multiple-choice answers earn question marks. The round score is the percentage of available marks earned.
- **Coding:** A recruiter manually enters the score; submitted code is not executed.
- **AI interview:** The AI is asked to rate technical/content quality, relevance, and completeness from 1–10 using the existing technical accuracy, communication, and problem-solving score fields. The stored ratings are averaged and converted to a 0–100 round score.
- **Final score:** The weighted average of enabled rounds that have a score. Weights for rounds without a score are excluded and the remaining weights are re-normalized.
- **Weights:** Configurable per job. Enabled round weights must total 100%; disabled rounds must have zero weight.

## 6. AI, ML, and NLP

- **Gemini (`gemini-pro`):** The code calls the Gemini API to generate interview follow-up questions, score answer content/relevance/completeness, and provide grammar feedback. This is generative-AI output, not a trained recruitment prediction model.
- **Faster-Whisper (`small`):** The interview audio endpoint uses this model for speech-to-text transcription.
- **Resume matching:** Uses extracted text, regular expressions, keyword checks, and a small explicit synonym map. It is not semantic AI matching or an ML suitability classifier.
- **Speaking pace:** Calculated from the transcribed word count and classified using fixed thresholds.
- **Current environment caveat:** The installed Gemini package was inspected and does not provide the `GenerativeModel` API the code expects. A valid Gemini API key and compatible dependency are needed before Gemini interview generation/scoring can be relied on. Whisper transcription also depends on the model being available; it was not verified in this environment.

## 7. Technology stack

- **Frontend:** HTML, CSS, JavaScript, Jinja templates, Bootstrap 4, Chart.js.
- **Backend:** Python and Flask.
- **Database:** SQLite by default, accessed through SQLAlchemy.
- **AI / speech:** Google Gemini API integration and Faster-Whisper.
- **Resume parsing:** `pdfminer.six` (PDF), `docx2txt` (DOCX), and custom Python text/regex/keyword extraction.
- **Authentication:** Flask-Login.
- **Reports:** CSV generation and browser print/save-as-PDF; no server-generated PDF file.

## 8. What recruiters see and control

Recruiters can view job and application metrics, inspect applicants' eligibility and round scores, use a per-job leaderboard, and open candidate reports. They control job and round configuration, assessment questions, coding problems, manual coding scores, and application statuses. CSV export and browser printing are available.

## 9. What candidates see and can do

Candidates can browse jobs, apply, check eligibility and application status, and view scores and reports for their applications. They can take enabled multiple-choice assessments, submit coding work, and start an enabled interview. They can upload a resume after signing in; resume upload is not part of the public registration form.

## 10. System outputs

For an application, the system can show eligibility and its reasons, individual available round scores, a weighted final score when at least one enabled round has a score, rank within that job, application status, and a printable report. Statuses are Applied, Under Review, Shortlisted, Selected, or Rejected; the recruiter controls shortlisting and selection. Recruiters can export application scores and statuses as CSV.

## 11. Important limitations

- Gemini interview generation, scoring, and grammar feedback are dependent on a valid API key and a compatible installed Gemini library. The currently inspected environment has an incompatible library version, so these AI functions should not be described as verified working there.
- Whisper transcription is implemented but depends on the model being installed/downloadable and was not verified in the current environment.
- Coding submissions are stored for review but are never compiled or executed; coding scores must be entered manually.
- Resume parsing and eligibility are best-effort heuristics. Formatting, missing details, or varied wording can cause information to be missed.
- The final score is provisional until scores are available for the configured rounds; uncompleted/unscored rounds are not counted as zero.
- The final hiring decision is not automated. Recruiters update statuses manually.
- The first admin account must be created from the trusted command line; public registration cannot create privileged accounts.
- Reports are printable in the browser, but the server does not generate PDF files.
