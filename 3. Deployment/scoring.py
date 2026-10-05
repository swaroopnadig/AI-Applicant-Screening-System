"""Eligibility and normalized round scoring for recruitment applications."""

import json
import re


ROUND_NAMES = {
    "resume_screening": "Resume screening",
    "aptitude": "Aptitude assessment",
    "technical": "Technical assessment",
    "coding": "Coding round",
    "ai_interview": "AI interview",
}

SKILL_EQUIVALENTS = {
    "ml": "machine learning",
    "machine-learning": "machine learning",
    "data analysis": "data analytics",
    "pandas": "data analytics",
    "numpy": "data analytics",
}


def default_rounds():
    return {
        "resume_screening": {"enabled": True, "weight": 20},
        "aptitude": {"enabled": True, "weight": 20},
        "technical": {"enabled": True, "weight": 20},
        "coding": {"enabled": False, "weight": 0},
        "ai_interview": {"enabled": True, "weight": 40},
    }


def parse_rounds(value):
    if not value:
        return default_rounds()
    rounds = json.loads(value) if isinstance(value, str) else value
    if not isinstance(rounds, dict):
        raise ValueError("Recruitment round configuration must be an object.")
    normalized = default_rounds()
    for name in ROUND_NAMES:
        configured = rounds.get(name, {})
        if not isinstance(configured, dict):
            raise ValueError("Invalid configuration for {}.".format(ROUND_NAMES[name]))
        weight = configured.get("weight", normalized[name]["weight"])
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 <= weight <= 100:
            raise ValueError("{} weight must be between 0 and 100.".format(ROUND_NAMES[name]))
        normalized[name] = {
            "enabled": bool(configured.get("enabled", normalized[name]["enabled"])),
            "weight": float(weight),
        }
    return normalized


def validate_rounds(rounds):
    enabled_weight = sum(
        item["weight"] for item in rounds.values() if item["enabled"]
    )
    if abs(enabled_weight - 100) > 1e-9:
        raise ValueError(
            "Enabled recruitment round weights must total 100% (current: {}).".format(
                round(enabled_weight, 2)
            )
        )
    if any(item["weight"] != 0 for item in rounds.values() if not item["enabled"]):
        raise ValueError("Disabled recruitment rounds must have a weight of 0%.")


def _candidate_data(candidate):
    raw = getattr(candidate, "parsed_text", None) or ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data, raw
    except (TypeError, ValueError):
        pass
    return {}, raw


def _normalized_skill(skill):
    value = re.sub(r"\s+", " ", str(skill).strip().lower())
    return SKILL_EQUIVALENTS.get(value, value)


def _contains_term(text, term):
    return re.search(
        r"(?<!\w)" + re.escape(str(term).lower()) + r"(?!\w)",
        text.lower(),
    ) is not None


def _as_number(value):
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _skill_matches(skill, resume_skills, text):
    normalized = _normalized_skill(skill)
    alternatives = {normalized}
    if normalized == "machine learning":
        alternatives.update(("ml", "machine-learning"))
    elif normalized == "python":
        if {"pandas", "numpy"}.intersection(resume_skills):
            return True
        alternatives.update(("pandas", "numpy"))
    elif normalized == "data analytics":
        alternatives.update(("data analysis", "pandas", "numpy"))
    return bool(alternatives.intersection(resume_skills)) or any(
        _contains_term(text, alternative) for alternative in alternatives
    )


def score_resume(candidate, job):
    data, text = _candidate_data(candidate)
    if not getattr(candidate, "resume_filename", None) and not text.strip():
        return 0.0
    candidate_skills = data.get("skills") or []
    if isinstance(candidate_skills, str):
        candidate_skills = [candidate_skills]
    resume_skills = {_normalized_skill(skill) for skill in candidate_skills}
    text_lower = text.lower()
    required_skills = []
    try:
        required_skills = json.loads(job.required_skills or "[]")
    except (TypeError, ValueError):
        required_skills = []
    if not required_skills:
        return 100.0
    matched = 0
    for skill in required_skills:
        if _skill_matches(skill, resume_skills, text_lower):
            matched += 1
    return round(100.0 * matched / len(required_skills), 2)


def evaluate_eligibility(candidate, job):
    """Return an explainable eligibility decision using available resume data."""
    data, text = _candidate_data(candidate)
    text_lower = text.lower()
    failures = []

    skills = data.get("skills") or []
    if isinstance(skills, str):
        skills = [skills]
    resume_skills = {_normalized_skill(skill) for skill in skills}
    try:
        required_skills = json.loads(job.required_skills or "[]")
    except (TypeError, ValueError):
        required_skills = []
    missing_skills = [
        skill for skill in required_skills
        if not _skill_matches(skill, resume_skills, text_lower)
    ]
    if missing_skills:
        failures.append("Missing required skills: {}.".format(", ".join(missing_skills)))

    min_experience = int(getattr(job, "minimum_experience", 0) or 0)
    if min_experience:
        experience = data.get("total_experience")
        if experience is None:
            exp_match = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years?", text_lower)
            experience = float(exp_match.group(1)) if exp_match else None
        else:
            experience = _as_number(experience)
        if experience is None:
            failures.append("Experience information is missing.")
        elif float(experience) < min_experience:
            failures.append(
                "Requires at least {} years of experience.".format(min_experience)
            )

    minimum_education = (getattr(job, "minimum_education", None) or "").lower()
    education = " ".join(
        str(data.get(key) or "") for key in ("degree", "college_name", "education")
    ).lower()
    education += " " + text_lower
    education_levels = {
        "high school": ("high school", "secondary school"),
        "bachelor's degree": ("bachelor", "b.sc", "b.tech", "b.e.", "undergraduate"),
        "master's degree": ("master", "m.sc", "m.tech", "m.e.", "postgraduate"),
        "phd": ("ph.d", "phd", "doctorate"),
    }
    if minimum_education:
        accepted = education_levels.get(minimum_education, (minimum_education,))
        level_order = ("high school", "bachelor's degree", "master's degree", "phd")
        required_index = level_order.index(minimum_education) if minimum_education in level_order else -1
        candidate_index = max(
            (index for index, level in enumerate(level_order)
             if any(_contains_term(education, term) for term in education_levels[level])),
            default=-1,
        )
        if not any(_contains_term(education, term) for term in accepted) and (
            required_index < 0 or candidate_index < required_index
        ):
            failures.append("Education requirement is not evidenced in the resume.")

    minimum_cgpa = getattr(job, "minimum_cgpa", None)
    if minimum_cgpa is not None:
        cgpa = data.get("cgpa") or data.get("percentage")
        if cgpa is None:
            match = re.search(r"\b(?:cgpa|gpa)\s*[:=]?\s*(\d+(?:\.\d+)?)", text_lower)
            if match:
                cgpa = float(match.group(1))
        else:
            cgpa = _as_number(cgpa)
        if cgpa is None:
            failures.append("CGPA/percentage information is missing.")
        elif float(cgpa) < float(minimum_cgpa):
            failures.append("CGPA/percentage is below the required minimum.")

    return {
        "eligible": not failures,
        "result": "Eligible" if not failures else "Not Eligible",
        "details": failures or ["All configured eligibility criteria are met."],
        "resume_score": score_resume(candidate, job),
    }


def calculate_final_score(rounds, scores):
    """Compute a weighted average from enabled rounds with submitted scores only."""
    weighted_total = 0.0
    applied_weight = 0.0
    for name, configuration in rounds.items():
        score = scores.get(name)
        if configuration["enabled"] and score is not None:
            normalized_score = min(100.0, max(0.0, float(score)))
            weight = float(configuration["weight"])
            weighted_total += normalized_score * weight
            applied_weight += weight
    if applied_weight == 0:
        return None
    return round(weighted_total / applied_weight, 2)
