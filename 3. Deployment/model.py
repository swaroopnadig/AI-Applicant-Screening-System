import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY environment variable not set. Please set it in .env file.")

genai.configure(api_key=api_key)

# Use the correct model name for the installed version
model = genai.GenerativeModel("gemini-pro")

def generate_output(prompt):
    """Generate non-streamed output (legacy function for compatibility)"""
    response = model.generate_content(prompt)
    return response.text

def generate_output_stream(prompt):
    """Generate streaming output for real-time response"""
    response = model.generate_content(prompt, stream=True)
    for chunk in response:
        if chunk.text:
            yield chunk.text

def generate_score(prompt):
    """Generate AI score evaluation with JSON response"""
    response = model.generate_content(prompt)
    return response.text