import json

import model


def test_interview_fallback_returns_a_follow_up_without_gemini(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    prompt = (
        "Previous Question:\nDescribe a project.\n\n"
        "Candidate Answer:\nI designed and tested a service because the old "
        "approach failed. The result improved response time."
    )

    assert list(model.generate_output_stream(prompt)) == [
        "What result did your approach achieve, and how did you measure its success?"
    ]


def test_interview_score_fallback_is_bounded_and_explained(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    prompt = (
        "Question: Explain how you designed the service.\n"
        "Candidate Answer: I designed and tested a service because the old "
        "approach failed. The result improved response time."
    )

    scores = json.loads(model.generate_score(prompt))

    assert all(
        type(scores[field]) is int and 1 <= scores[field] <= 10
        for field in ("technical_accuracy", "communication", "problem_solving")
    )
    assert "do not verify technical correctness" in scores["feedback"]


def test_grammar_fallback_does_not_claim_to_correct(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    output = model.generate_output(
        "Correct “I has a project” to standard English and place the results "
        "in “Correct Text:”"
    )

    assert "does not provide grammar correction" in output
