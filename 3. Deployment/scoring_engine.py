"""Deterministic composite applicant scoring service."""

import json
import re
from datetime import datetime

from database import AIScore, CandidateEvaluation, InterviewSession


def _clamp(value):
    return max(0.0, min(100.0, float(value or 0.0)))


def _normalise_weights(job):
    raw = {
        "resume": float(getattr(job, "weight_resume", 30.0) or 0.0),
        "skills": float(getattr(job, "weight_skills", 40.0) or 0.0),
        "ai": float(getattr(job, "weight_ai", 30.0) or 0.0),
    }
    total = sum(raw.values())
    if total <= 0:
        return {"resume": 1 / 3, "skills": 1 / 3, "ai": 1 / 3}
    return {key: value / total for key, value in raw.items()}


def _tokens(value):
    return {token.lower() for token in re.findall(r"[A-Za-z0-9+#.]+", value or "")}


def _resume_score(job, parsed_resume):
    resume_text = " ".join(
        [
            parsed_resume.get("parsed_text", ""),
            " ".join(parsed_resume.get("skills", []) or []),
            " ".join(parsed_resume.get("experience", []) or []),
            " ".join(parsed_resume.get("college_name", []) or []),
        ]
    )
    job_text = " ".join([job.title or "", job.description or "", job.minimum_education or ""])
    required = _tokens(job_text)
    if not required:
        return 0.0
    return _clamp(len(required.intersection(_tokens(resume_text))) / len(required) * 100)


def _skills_score(job, parsed_resume):
    try:
        required = json.loads(job.required_skills or "[]")
    except (TypeError, ValueError):
        required = []
    required = [skill.strip().lower() for skill in required if skill and skill.strip()]
    candidate = _tokens(" ".join(parsed_resume.get("skills", []) or []) + " " + parsed_resume.get("parsed_text", ""))
    if not required:
        return 100.0
    matched = sum(1 for skill in required if _tokens(skill).issubset(candidate))
    return _clamp(matched / len(required) * 100)


def _ai_score(db, interview_session_id, candidate_id):
    latest = db.get(InterviewSession, interview_session_id) if interview_session_id else None
    if latest is None:
        latest = (
            db.query(InterviewSession)
            .filter_by(candidate_id=candidate_id)
            .order_by(InterviewSession.created_at.desc())
            .first()
        )
    if not latest:
        return 0.0
    scores = db.query(AIScore).filter_by(session_id=latest.id).all()
    if not scores:
        return 0.0
    values = [
        (float(score.technical_accuracy or 0) + float(score.communication or 0) + float(score.problem_solving or 0))
        / 30
        * 100
        for score in scores
    ]
    return _clamp(sum(values) / len(values))


def calculate_composite_score(db, candidate_id, job_id=None, interview_session_id=None, application_id=None):
    """Calculate and stage one application's score in the caller's transaction."""
    from database import Candidate, Job, JobApplication

    candidate = db.get(Candidate, candidate_id)
    application = (
        db.get(JobApplication, application_id) if application_id else
        db.query(JobApplication).filter_by(candidate_id=candidate_id, job_id=job_id).first()
    )
    if not application and candidate and job_id:
        application = JobApplication(job_id=job_id, candidate_id=candidate_id, status="Interviewed")
        db.add(application)
        db.flush()
    if not application:
        raise ValueError("Application not found")
    job = db.get(Job, job_id or application.job_id)
    if not candidate or not job:
        raise ValueError("Application has incomplete candidate or job data")

    parsed_resume = {}
    try:
        parsed_resume = json.loads(candidate.parsed_text or "{}")
    except (TypeError, ValueError):
        parsed_resume = {}
    parsed_resume["parsed_text"] = candidate.parsed_text or ""

    resume_score = _resume_score(job, parsed_resume)
    skills_score = _skills_score(job, parsed_resume)
    ai_response_score = _ai_score(db, interview_session_id, candidate.id)
    weights = _normalise_weights(job)
    final_score = _clamp(
        weights["resume"] * resume_score
        + weights["skills"] * skills_score
        + weights["ai"] * ai_response_score
    )
    status = "Shortlisted" if final_score >= float(getattr(job, "cutoff_score", 75.0) or 75.0) else "Evaluated"

    evaluation = db.query(CandidateEvaluation).filter_by(application_id=application.id).first()
    if not evaluation:
        evaluation = CandidateEvaluation(application_id=application.id)
        db.add(evaluation)
    evaluation.resume_score = resume_score
    evaluation.skills_score = skills_score
    evaluation.ai_response_score = ai_response_score
    evaluation.final_weighted_score = final_score
    evaluation.status = status
    evaluation.evaluated_at = datetime.utcnow()
    evaluation.breakdown_json = json.dumps({"weights": weights, "scores": {
        "resume": resume_score, "skills": skills_score, "ai": ai_response_score,
    }})
    db.flush()
    return evaluation
