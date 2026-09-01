#!/usr/bin/env python
"""
Production server startup script with model pre-caching.
Uses waitress WSGI server instead of Flask development server.
Pre-downloads Faster-Whisper model to avoid startup delays.
"""

import sys
import os
from pathlib import Path

print("[*] Starting AI Interview Platform with optimizations...")

# Pre-cache the Faster-Whisper model
print("[*] Pre-caching Faster-Whisper model (this may take 1-2 minutes on first run)...")
try:
    from faster_whisper import WhisperModel
    # Pre-load the "small" model to cache it
    _model_cache = WhisperModel(model_size_or_path="small")
    print("[✓] Faster-Whisper model cached successfully")
except Exception as e:
    print(f"[!] Warning: Could not pre-cache Whisper model: {e}")
    print("[*] Model will be downloaded on first transcription request instead")

# Pre-cache Gemini model (try both SDK versions)
print("[*] Pre-caching Gemini API client...")
try:
    try:
        import google.genai as genai
        print("[✓] Google genai SDK available")
    except ImportError:
        import google.generativeai as genai
        print("[✓] Google generativeai SDK available (legacy)")
except Exception as e:
    print(f"[!] Warning: Could not initialize Gemini: {e}")

# Pre-cache resume parser model
print("[*] Pre-caching resume parser...")
try:
    from resume_parser import parse_resume
    print("[✓] Resume parser ready")
except Exception as e:
    print(f"[!] Warning: Could not pre-cache resume parser: {e}")

print("[*] All models cached. Starting Flask application with waitress...")

# Import and run the Flask app with waitress
try:
    from app import app
    from waitress import serve
    
    # Serve on localhost:5000 (change host/port as needed for deployment)
    print("[*] Flask app loaded. Running on http://127.0.0.1:5000")
    print("[*] Press CTRL+C to stop the server")
    
    # Use waitress instead of Flask dev server
    # threads=4 allows handling multiple requests concurrently
    # _quiet=False shows access logs
    serve(
        app,
        host='127.0.0.1',
        port=5000,
        threads=4,
        _quiet=False
    )
    
except Exception as e:
    print(f"[!] Error starting server: {e}", file=sys.stderr)
    sys.exit(1)
