import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
DEPLOY_DIR = os.path.join(ROOT, "3. Deployment")
if DEPLOY_DIR not in sys.path:
    sys.path.insert(0, DEPLOY_DIR)

import app as app_module


def test_valid_answer_returns_score_and_question():
    client = app_module.app.test_client()
    payload = {
        "candidate_name": "Aisha",
        "question": "Tell me about yourself.",
        "answer": (
            "I am a Python developer with 4 years of experience building Flask APIs, "
            "SQL-backed services, and production deployment workflows using Docker and AWS."
        ),
    }

    response = client.post("/api/interview/evaluate", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data.get("score"), int)
    assert 0 <= data["score"] <= 100
    assert data["feedback"]
    assert data["next_question"]


def test_timeout_falls_back_cleanly(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise TimeoutError("quota exceeded")

    monkeypatch.setattr(app_module, "_call_evaluation_provider", raise_timeout)
    client = app_module.app.test_client()
    response = client.post(
        "/api/interview/evaluate",
        json={
            "answer": "I built Python APIs and worked with SQL and deployment workflows.",
            "question": "Tell me about yourself.",
        },
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["provider"] == "fallback"
    assert 0 <= data["score"] <= 100


def test_blank_answer_rejected():
    client = app_module.app.test_client()
    response = client.post("/api/interview/evaluate", json={"answer": "   ", "question": "Tell me about yourself."})
    assert response.status_code == 400
    assert "required" in response.get_json()["error"].lower()
