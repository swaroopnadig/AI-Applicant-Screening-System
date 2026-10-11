import os
import json
import re
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

_model = None


def _rule_based_fallback_enabled():
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    return (
        not api_key
        or api_key.lower() in ("your_gemini_api_key_here", "changeme")
        or not hasattr(genai, "GenerativeModel")
    )


def _prompt_section(prompt, label):
    match = re.search(
        r"^{}\s*:?[ \t]*(.*?)(?=^[A-Z][A-Za-z ]+\s*:|\Z)".format(
            re.escape(label)
        ),
        prompt,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _fallback_question(prompt):
    answer = _prompt_section(prompt, "Candidate Answer")
    words = re.findall(r"[A-Za-z0-9']+", answer)
    if len(words) < 15:
        return "Could you add a specific example and explain your individual contribution?"
    answer_lower = answer.lower()
    if "result" in answer_lower or "outcome" in answer_lower:
        return "What result did your approach achieve, and how did you measure its success?"
    if any(term in answer_lower for term in ("challenge", "problem", "issue", "difficult")):
        return "What result did your approach achieve, and how did you measure its success?"
    return "What was the main challenge, and what would you improve in your approach?"


def _fallback_scores(prompt):
    answer = _prompt_section(prompt, "Candidate Answer")
    question = _prompt_section(prompt, "Question")
    answer_words = {
        word.lower() for word in re.findall(r"[A-Za-z]{3,}", answer)
    }
    question_words = {
        word.lower() for word in re.findall(r"[A-Za-z]{3,}", question)
    } - {"what", "when", "where", "which", "could", "would", "about", "tell"}

    word_count = len(re.findall(r"[A-Za-z0-9']+", answer))
    reasoning_terms = (
        "because", "therefore", "approach", "implemented", "tested",
        "result", "measured", "designed", "debugged",
    )
    reasoning_count = sum(term in answer.lower() for term in reasoning_terms)
    overlap = len(answer_words.intersection(question_words))
    relevance = (
        min(10, 1 + round(9 * overlap / max(1, min(len(question_words), 8))))
        if question_words else 5
    )
    detail = min(10, max(1, 1 + word_count // 15 + reasoning_count))
    completeness = min(10, max(1, 1 + word_count // 12))
    return json.dumps({
        "technical_accuracy": detail,
        "communication": relevance,
        "problem_solving": completeness,
        "feedback": (
            "Rule-based fallback: scores reflect answer detail, question-word "
            "overlap, and answer length; they do not verify technical correctness."
        ),
    })


def _get_model():
    global _model
    if _model is not None:
        return _model

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is required for AI interview features. "
            "Set it in the deployment environment or .env file."
        )
    if not hasattr(genai, "GenerativeModel"):
        raise RuntimeError(
            "The installed google-generativeai package does not expose "
            "GenerativeModel; reinstall the pinned project dependencies."
        )

    genai.configure(api_key=api_key)
    _model = genai.GenerativeModel("gemini-pro")
    return _model

def generate_output(prompt):
    """Generate non-streamed output (legacy function for compatibility)"""
    if _rule_based_fallback_enabled():
        if "correct" in prompt.lower() and "standard english" in prompt.lower():
            return (
                "Gemini is unavailable; the rule-based fallback does not "
                "provide grammar correction."
            )
        return _fallback_question(prompt)
    response = _get_model().generate_content(prompt)
    return response.text

def generate_output_stream(prompt):
    """Generate streaming output for real-time response"""
    if _rule_based_fallback_enabled():
        yield _fallback_question(prompt)
        return
    response = _get_model().generate_content(prompt, stream=True)
    for chunk in response:
        if chunk.text:
            yield chunk.text

def generate_score(prompt):
    """Generate AI score evaluation or a transparent deterministic fallback."""
    if _rule_based_fallback_enabled():
        return _fallback_scores(prompt)
    response = _get_model().generate_content(prompt)
    return response.text