import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
model_name = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

legacy_model = None
client = None

try:
    from google import genai as google_genai
    client = google_genai.Client(api_key=api_key)
except Exception:
    client = None

if client is None:
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        legacy_model = genai.GenerativeModel(model_name)
    except Exception:
        legacy_model = None


def _fallback_text(message="AI service unavailable. Please verify the Gemini API key in the project .env file."):
    return message


def generate_output(prompt):
    """Generate non-streamed output with compatibility for both Gemini SDK versions."""
    try:
        if client is not None:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if hasattr(response, "text"):
                return response.text
            if hasattr(response, "candidates") and response.candidates:
                parts = response.candidates[0].content.parts
                text_parts = []
                for part in parts:
                    if hasattr(part, "text") and part.text:
                        text_parts.append(part.text)
                if text_parts:
                    return "".join(text_parts)
            return _fallback_text()

        if legacy_model is not None:
            response = legacy_model.generate_content(prompt)
            return getattr(response, "text", _fallback_text())

        return _fallback_text()
    except Exception:
        return _fallback_text()


def generate_output_stream(prompt):
    """Generate streaming output for real-time response."""
    try:
        if client is not None:
            response = client.models.generate_content_stream(
                model=model_name,
                contents=prompt,
            )
            for chunk in response:
                text = getattr(chunk, "text", None)
                if text:
                    yield text
            return

        if legacy_model is not None:
            response = legacy_model.generate_content(prompt, stream=True)
            for chunk in response:
                text = getattr(chunk, "text", None)
                if text:
                    yield text
            return
    except Exception:
        yield _fallback_text()


def generate_score(prompt):
    """Generate AI score evaluation with JSON response."""
    try:
        if client is not None:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if hasattr(response, "text"):
                return response.text
            if hasattr(response, "candidates") and response.candidates:
                parts = response.candidates[0].content.parts
                text_parts = []
                for part in parts:
                    if hasattr(part, "text") and part.text:
                        text_parts.append(part.text)
                if text_parts:
                    return "".join(text_parts)
        if legacy_model is not None:
            response = legacy_model.generate_content(prompt)
            return getattr(response, "text", _fallback_text())
        return _fallback_text()
    except Exception:
        return _fallback_text()