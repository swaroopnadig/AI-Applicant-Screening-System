import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

_model = None


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
    response = _get_model().generate_content(prompt)
    return response.text

def generate_output_stream(prompt):
    """Generate streaming output for real-time response"""
    response = _get_model().generate_content(prompt, stream=True)
    for chunk in response:
        if chunk.text:
            yield chunk.text

def generate_score(prompt):
    """Generate AI score evaluation with JSON response"""
    response = _get_model().generate_content(prompt)
    return response.text