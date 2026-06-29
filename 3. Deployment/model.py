import google.generativeai as genai

genai.configure(api_key=None)

model = genai.GenerativeModel("gemini-2.5-flash")

def generate_output(prompt):
    response = model.generate_content(prompt)
    return response.text