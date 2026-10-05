import json
import unittest
from types import SimpleNamespace

from scoring import (
    calculate_final_score,
    evaluate_eligibility,
    parse_rounds,
    score_resume,
    validate_rounds,
)
from resume_parser.resume_parser_service import ResumeParserService


class RecruitmentScoringTests(unittest.TestCase):
    def test_enabled_round_weights_must_total_one_hundred(self):
        rounds = parse_rounds({
            "resume_screening": {"enabled": True, "weight": 40},
            "aptitude": {"enabled": True, "weight": 60},
            "technical": {"enabled": False, "weight": 0},
            "coding": {"enabled": False, "weight": 0},
            "ai_interview": {"enabled": False, "weight": 0},
        })
        validate_rounds(rounds)
        rounds["aptitude"]["weight"] = 59
        with self.assertRaises(ValueError):
            validate_rounds(rounds)

    def test_final_score_renormalizes_only_scored_enabled_rounds(self):
        rounds = parse_rounds({
            "resume_screening": {"enabled": True, "weight": 50},
            "aptitude": {"enabled": True, "weight": 30},
            "technical": {"enabled": True, "weight": 20},
            "coding": {"enabled": False, "weight": 0},
            "ai_interview": {"enabled": False, "weight": 0},
        })
        self.assertEqual(
            calculate_final_score(rounds, {
                "resume_screening": 90,
                "aptitude": 50,
                "technical": None,
            }),
            75,
        )
        self.assertIsNone(calculate_final_score(rounds, {}))

    def test_resume_score_recognizes_related_skills(self):
        candidate = SimpleNamespace(
            resume_filename="resume.pdf",
            parsed_text=json.dumps({
                "skills": ["Machine Learning", "Pandas", "NumPy"],
            }),
        )
        job = SimpleNamespace(
            required_skills=json.dumps(["ML", "Data Analysis", "Python"]),
        )
        self.assertEqual(score_resume(candidate, job), 100)

    def test_eligibility_reports_missing_required_information(self):
        candidate = SimpleNamespace(resume_filename="", parsed_text="")
        job = SimpleNamespace(
            required_skills=json.dumps(["Python"]),
            minimum_experience=2,
            minimum_education="Bachelor's Degree",
            minimum_cgpa=7.0,
        )
        result = evaluate_eligibility(candidate, job)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["result"], "Not Eligible")
        self.assertEqual(result["resume_score"], 0)
        self.assertGreaterEqual(len(result["details"]), 3)

    def test_eligible_candidate_with_resume_meets_configured_criteria(self):
        candidate = SimpleNamespace(
            resume_filename="resume.pdf",
            parsed_text=json.dumps({
                "skills": ["Python", "Machine Learning"],
                "degree": "Bachelor of Engineering",
                "total_experience": 3,
                "cgpa": 8.1,
            }),
        )
        job = SimpleNamespace(
            required_skills=json.dumps(["Python", "ML"]),
            minimum_experience=2,
            minimum_education="Bachelor's Degree",
            minimum_cgpa=8.0,
        )
        result = evaluate_eligibility(candidate, job)
        self.assertTrue(result["eligible"], result["details"])
        self.assertEqual(result["resume_score"], 100)

    def test_resume_parser_extracts_basic_structured_credentials(self):
        text = "Bachelor of Engineering, CGPA: 8.25/10, Percentage: 82%"
        self.assertEqual(
            ResumeParserService._extract_degree(text), "Bachelor's Degree"
        )
        self.assertEqual(
            ResumeParserService._extract_numeric_credential(
                text, r"\b(?:cgpa|gpa)\s*[:=]?\s*(\d+(?:\.\d+)?)"
            ),
            8.25,
        )



if __name__ == "__main__":
    unittest.main()
