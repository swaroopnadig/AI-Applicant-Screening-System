from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response, stream_with_context
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from model import generate_output, generate_output_stream, generate_score
from faster_whisper import WhisperModel
import os
import json
import uuid
from resume_parser import parse_resume, FileValidationError, ResumeParsingError
from database import init_db, get_db, Resume, Candidate, InterviewSession, AIScore, User, Job, JobApplication

model_audio = WhisperModel(model_size_or_path="small")

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max file size
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize database
init_db()

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    try:
        return db.query(User).get(int(user_id))
    finally:
        db.close()


# File upload validation
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}
ALLOWED_MIME_TYPES = {
    'pdf': ['application/pdf'],
    'docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    'txt': ['text/plain']
}

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def validate_mime_type(file, filename):
    """Validate file MIME type"""
    ext = filename.rsplit('.', 1)[1].lower()
    file.seek(0)
    header = file.read(2048)
    file.seek(0)
    
    # For PDF files, check magic bytes
    if ext == 'pdf':
        return header.startswith(b'%PDF')
    
    # For text files, check if it's readable text
    if ext == 'txt':
        try:
            header.decode('utf-8')
            return True
        except UnicodeDecodeError:
            return False
    
    # For DOCX, it's harder to validate without parsing, so we'll rely on extension
    # but could add zip header check if needed
    if ext == 'docx':
        return True
    
    return False

name = []
position = []
prev_q = []
flag = 0
feedback = ["Grammatical correction here"]
pace = 0.0

@app.route("/", methods=["GET", "POST"])
def index():
    return render_template('index.html')

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")
        role = request.form.get("role", "candidate")
        
        if not username or not email or not password:
            return render_template('register.html', error="All fields are required")
        
        db = get_db()
        try:
            # Check if user already exists
            if db.query(User).filter_by(username=username).first():
                return render_template('register.html', error="Username already exists")
            if db.query(User).filter_by(email=email).first():
                return render_template('register.html', error="Email already exists")
            
            # Create new user
            password_hash = generate_password_hash(password)
            new_user = User(username=username, email=email, password_hash=password_hash, role=role)
            db.add(new_user)
            db.commit()
            
            return redirect(url_for('login'))
        except Exception as e:
            db.rollback()
            return render_template('register.html', error=f"Registration failed: {str(e)}")
        finally:
            db.close()
    
    return render_template('register.html')

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if not username or not password:
            return render_template('login.html', error="Username and password are required")
        
        db = get_db()
        try:
            user = db.query(User).filter_by(username=username).first()
            if user and check_password_hash(user.password_hash, password):
                login_user(user)
                next_page = request.args.get('next')
                if user.role in ['admin', 'recruiter']:
                    return redirect(next_page or url_for('admin'))
                else:
                    return redirect(next_page or url_for('index'))
            else:
                return render_template('login.html', error="Invalid username or password")
        finally:
            db.close()
    
    return render_template('login.html')

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# Job Management Routes

@app.route("/jobs")
@login_required
def jobs():
    """List all available jobs for candidates"""
    db = get_db()
    try:
        jobs = db.query(Job).order_by(Job.created_at.desc()).all()
        return render_template('jobs.html', jobs=jobs)
    finally:
        db.close()

@app.route("/jobs/<int:job_id>")
@login_required
def job_details(job_id):
    """View job details"""
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return render_template('jobs.html', error="Job not found", jobs=db.query(Job).all())
        
        # Check if candidate has already applied
        has_applied = False
        if current_user.role == 'candidate':
            # Get candidate ID from username
            candidate = db.query(Candidate).filter_by(name=current_user.username).first()
            if candidate:
                application = db.query(JobApplication).filter_by(job_id=job_id, candidate_id=candidate.id).first()
                has_applied = application is not None
        
        job.required_skills_list = json.loads(job.required_skills) if job.required_skills else []
        return render_template('job_details.html', job=job, has_applied=has_applied)
    finally:
        db.close()

@app.route("/jobs/<int:job_id>/apply", methods=["POST"])
@login_required
def apply_job(job_id):
    """Apply for a job"""
    if current_user.role != 'candidate':
        return redirect(url_for('jobs'))
    
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return redirect(url_for('jobs'))
        
        # Get candidate ID
        candidate = db.query(Candidate).filter_by(name=current_user.username).first()
        if not candidate:
            return redirect(url_for('jobs'))
        
        # Check if already applied
        existing = db.query(JobApplication).filter_by(job_id=job_id, candidate_id=candidate.id).first()
        if existing:
            return redirect(url_for('job_details', job_id=job_id))
        
        # Create application
        application = JobApplication(job_id=job_id, candidate_id=candidate.id)
        db.add(application)
        db.commit()
        
        return redirect(url_for('job_details', job_id=job_id))
    finally:
        db.close()

@app.route("/admin/jobs")
@login_required
def admin_jobs():
    """Manage jobs for recruiters/admin"""
    if current_user.role not in ['admin', 'recruiter']:
        return redirect(url_for('index'))
    
    db = get_db()
    try:
        jobs = db.query(Job).order_by(Job.created_at.desc()).all()
        return render_template('admin_jobs.html', jobs=jobs)
    finally:
        db.close()

@app.route("/admin/jobs/create", methods=["GET", "POST"])
@login_required
def create_job():
    """Create a new job"""
    if current_user.role not in ['admin', 'recruiter']:
        return redirect(url_for('index'))
    
    if request.method == "POST":
        title = request.form.get("title")
        company = request.form.get("company")
        location = request.form.get("location")
        description = request.form.get("description")
        required_skills = request.form.get("required_skills")
        minimum_experience = request.form.get("minimum_experience", 0)
        minimum_education = request.form.get("minimum_education")
        weight_skills = request.form.get("weight_skills", 40)
        weight_experience = request.form.get("weight_experience", 30)
        weight_education = request.form.get("weight_education", 30)
        
        if not title or not company:
            return render_template('job_form.html', error="Title and company are required")
        
        # Validate weightage sums to 100
        total_weight = int(weight_skills) + int(weight_experience) + int(weight_education)
        if total_weight != 100:
            return render_template('job_form.html', error=f"Total weightage must be 100 (current: {total_weight})")
        
        db = get_db()
        try:
            # Convert skills to JSON array
            skills_array = [skill.strip() for skill in required_skills.split(',')] if required_skills else []
            
            new_job = Job(
                title=title,
                company=company,
                location=location,
                description=description,
                required_skills=json.dumps(skills_array),
                minimum_experience=int(minimum_experience),
                minimum_education=minimum_education,
                weight_skills=int(weight_skills),
                weight_experience=int(weight_experience),
                weight_education=int(weight_education),
                created_by=current_user.id
            )
            db.add(new_job)
            db.commit()
            
            return redirect(url_for('admin_jobs'))
        except Exception as e:
            db.rollback()
            return render_template('job_form.html', error=f"Failed to create job: {str(e)}")
        finally:
            db.close()
    
    return render_template('job_form.html')

@app.route("/admin/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def edit_job(job_id):
    """Edit an existing job"""
    if current_user.role not in ['admin', 'recruiter']:
        return redirect(url_for('index'))
    
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return redirect(url_for('admin_jobs'))
        
        if request.method == "POST":
            job.title = request.form.get("title")
            job.company = request.form.get("company")
            job.location = request.form.get("location")
            job.description = request.form.get("description")
            required_skills = request.form.get("required_skills")
            job.minimum_experience = int(request.form.get("minimum_experience", 0))
            job.minimum_education = request.form.get("minimum_education")
            job.weight_skills = int(request.form.get("weight_skills", 40))
            job.weight_experience = int(request.form.get("weight_experience", 30))
            job.weight_education = int(request.form.get("weight_education", 30))
            
            # Validate weightage
            total_weight = job.weight_skills + job.weight_experience + job.weight_education
            if total_weight != 100:
                return render_template('job_form.html', job=job, error=f"Total weightage must be 100 (current: {total_weight})")
            
            # Convert skills to JSON array
            skills_array = [skill.strip() for skill in required_skills.split(',')] if required_skills else []
            job.required_skills = json.dumps(skills_array)
            
            db.commit()
            return redirect(url_for('admin_jobs'))
        
        job.required_skills_list = json.loads(job.required_skills) if job.required_skills else []
        return render_template('job_form.html', job=job)
    finally:
        db.close()

@app.route("/admin/jobs/<int:job_id>/delete", methods=["POST"])
@login_required
def delete_job(job_id):
    """Delete a job"""
    if current_user.role not in ['admin', 'recruiter']:
        return redirect(url_for('index'))
    
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if job:
            db.delete(job)
            db.commit()
        return redirect(url_for('admin_jobs'))
    finally:
        db.close()

@app.route("/admin")
@login_required
def admin():
    """Recruiter admin dashboard"""
    db = get_db()
    try:
        # Get all candidates with their interview sessions and scores
        candidates = db.query(Candidate).all()
        candidates_data = []
        
        for candidate in candidates:
            # Get latest interview session
            latest_session = db.query(InterviewSession).filter_by(candidate_id=candidate.id).order_by(InterviewSession.created_at.desc()).first()
            
            if latest_session:
                # Calculate average scores
                scores = db.query(AIScore).filter_by(session_id=latest_session.id).all()
                avg_tech = sum(s.technical_accuracy for s in scores) / len(scores) if scores else 0
                avg_comm = sum(s.communication for s in scores) / len(scores) if scores else 0
                avg_problem = sum(s.problem_solving for s in scores) / len(scores) if scores else 0
                
                candidates_data.append({
                    'id': candidate.id,
                    'name': candidate.name,
                    'email': candidate.email,
                    'resume_filename': candidate.resume_filename,
                    'position': latest_session.position,
                    'status': latest_session.status,
                    'avg_technical': round(avg_tech, 1),
                    'avg_communication': round(avg_comm, 1),
                    'avg_problem_solving': round(avg_problem, 1),
                    'session_id': latest_session.id,
                    'created_at': latest_session.created_at.strftime('%Y-%m-%d %H:%M')
                })
            else:
                candidates_data.append({
                    'id': candidate.id,
                    'name': candidate.name,
                    'email': candidate.email,
                    'resume_filename': candidate.resume_filename,
                    'position': 'N/A',
                    'status': 'No Session',
                    'avg_technical': 0,
                    'avg_communication': 0,
                    'avg_problem_solving': 0,
                    'session_id': None,
                    'created_at': candidate.created_at.strftime('%Y-%m-%d %H:%M')
                })
    except Exception as e:
        print(f"Error fetching admin data: {e}")
        candidates_data = []
    finally:
        db.close()
    
    return render_template('admin.html', candidates=candidates_data)

@app.route("/admin/candidate/<int:session_id>")
def candidate_details(session_id):
    """Get detailed candidate information and scores"""
    db = get_db()
    try:
        session = db.query(InterviewSession).filter_by(id=session_id).first()
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        
        candidate = db.query(Candidate).filter_by(id=session.candidate_id).first()
        scores = db.query(AIScore).filter_by(session_id=session_id).all()
        
        scores_data = [{
            'question': s.question,
            'answer': s.answer,
            'technical_accuracy': s.technical_accuracy,
            'communication': s.communication,
            'problem_solving': s.problem_solving,
            'feedback': s.feedback,
            'created_at': s.created_at.strftime('%Y-%m-%d %H:%M')
        } for s in scores]
        
        return jsonify({
            'candidate': {
                'name': candidate.name,
                'email': candidate.email,
                'resume_filename': candidate.resume_filename,
                'position': session.position,
                'status': session.status,
                'created_at': session.created_at.strftime('%Y-%m-%d %H:%M')
            },
            'scores': scores_data
        })
    except Exception as e:
        print(f"Error fetching candidate details: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()

@app.route("/home", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        username = request.form["username"]
        pos = request.form["position"]
        print(username)
        print(pos)
        name.append(username)
        position.append(pos)
        
        # Generate session ID
        if 'session_id' not in session:
            session['session_id'] = str(os.urandom(16).hex())
        
        session_id = session['session_id']
        
        # Create candidate and interview session in database
        db = get_db()
        try:
            # Check if candidate already exists
            existing_candidate = db.query(Candidate).filter_by(name=username).first()
            if not existing_candidate:
                candidate = Candidate(name=username, resume_filename="", parsed_text="")
                db.add(candidate)
                db.commit()
                db.refresh(candidate)
                candidate_id = candidate.id
            else:
                candidate_id = existing_candidate.id
            
            # Create interview session
            interview_session = InterviewSession(
                candidate_id=candidate_id,
                session_id=session_id,
                position=pos,
                status="Active"
            )
            db.add(interview_session)
            db.commit()
            db.refresh(interview_session)
            session['interview_session_id'] = interview_session.id
        except Exception as db_error:
            db.rollback()
            print(f"Database error: {db_error}")
        finally:
            db.close()
        
        # Handle resume upload with security validation
        parsed_data = None
        if 'resume' in request.files and request.files['resume'].filename:
            resume_file = request.files['resume']
            if resume_file.filename:
                # Validate file extension
                if not allowed_file(resume_file.filename):
                    return render_template('home.html', name=name[0], error="Invalid file type. Only PDF, DOCX, and TXT files are allowed.")
                
                # Validate MIME type (magic bytes check)
                if not validate_mime_type(resume_file, resume_file.filename):
                    return render_template('home.html', name=name[0], error="File content does not match the file extension. Please upload a valid file.")
                
                # Generate secure filename with UUID to prevent collisions
                original_filename = secure_filename(resume_file.filename)
                file_ext = original_filename.rsplit('.', 1)[1].lower()
                unique_filename = f"{uuid.uuid4().hex}_{original_filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                
                # Save file
                resume_file.save(filepath)
                
                try:
                    # Parse resume using existing parser
                    parsed_data = parse_resume(filepath)
                    
                    # Check if parsing returned valid data
                    if not parsed_data or not isinstance(parsed_data, dict):
                        raise ResumeParsingError("Parser returned invalid data")
                    
                    # Validate parsed data has minimum required fields
                    if not parsed_data.get('email') and not parsed_data.get('mobile_number'):
                        return render_template('home.html', name=name[0], error="Resume must contain at least an email or phone number.")
                    
                    print(f"Resume parsed successfully: {parsed_data.get('email')}, {len(parsed_data.get('skills', []))} skills")
                    
                    # Update candidate with resume data
                    db = get_db()
                    try:
                        candidate = db.query(Candidate).filter_by(name=username).first()
                        if candidate:
                            candidate.resume_filename = unique_filename  # Use secure filename
                            candidate.email = parsed_data.get('email', candidate.email or '')
                            candidate.parsed_text = json.dumps(parsed_data)
                            db.commit()
                    except Exception as db_error:
                        db.rollback()
                        print(f"Database error updating candidate: {db_error}")
                        return render_template('home.html', name=name[0], error="Failed to save resume data to database.")
                    finally:
                        db.close()
                        
                    # Store in legacy Resume table for compatibility
                    db = get_db()
                    try:
                        existing_resume = db.query(Resume).filter_by(session_id=session_id).first()
                        if existing_resume:
                            existing_resume.username = username
                            existing_resume.position = pos
                            existing_resume.filename = unique_filename  # Use secure filename
                            existing_resume.extracted_json = json.dumps(parsed_data)
                        else:
                            new_resume = Resume(
                                session_id=session_id,
                                username=username,
                                position=pos,
                                filename=unique_filename,  # Use secure filename
                                extracted_json=json.dumps(parsed_data)
                            )
                            db.add(new_resume)
                        db.commit()
                    except Exception as db_error:
                        db.rollback()
                        print(f"Database error updating resume table: {db_error}")
                        # Non-critical error, continue
                    finally:
                        db.close()
                        
                except FileValidationError as e:
                    print(f"Resume validation error: {e}")
                    # Clean up uploaded file
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return render_template('home.html', name=name[0], error=f"Resume validation failed: {str(e)}")
                except ResumeParsingError as e:
                    print(f"Resume parsing error: {e}")
                    # Clean up uploaded file
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return render_template('home.html', name=name[0], error=f"Resume parsing failed: {str(e)}. Please ensure the file is not corrupted.")
                except Exception as e:
                    print(f"Unexpected error during resume processing: {e}")
                    # Clean up uploaded file
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return render_template('home.html', name=name[0], error=f"An error occurred while processing your resume. Please try again.")
        
        return render_template('home.html', name=name[0], resume_data=parsed_data)
    return render_template('home.html')

@app.route("/get_flag", methods=["GET"])
def get_flag():
    global feedback
    if len(feedback)>0:
        return jsonify({'flag': feedback[-1]})
    else :
        return jsonify({'flag': ''})

@app.route("/get", methods=["GET", "POST"])
def chat_():
    msg = request.form["msg"]
    input = msg
    return get_Chat_response(input)

@app.route("/get_stream", methods=["POST"])
def chat_stream():
    """Streaming endpoint for real-time response"""
    msg = request.form["msg"]
    return Response(stream_with_context(get_Chat_response_stream(msg)), mimetype='text/plain')

@app.route('/upload', methods=['POST'])
def upload_audio():
    if 'audio' in request.files:
        audio_file = request.files['audio']
        audio_file.save('audio.wav')
        print("Audio saved successfully")
        return 'Audio uploaded successfully', 200
    else:
        print("Audio not saved")
        return 'No audio file received', 400

DEFAULT_CHUNK_LENGTH = 10

@app.route('/get_text', methods=['GET'])
def get_text():
    global pace
    audio_path = "./audio.wav"
    result = model_audio.transcribe(audio_path)
    segments, info = result
    print("Detected language '%s' with probability %f" % (info.language, info.language_probability))
    text = ""
    for segment in segments:
        text += segment.text
    pace = calculate_speaking_pace(text, chunk_length=DEFAULT_CHUNK_LENGTH)
    return text

@app.route('/get_pace', methods=['GET'])
def get_pace():
    global pace
    global flag
    output = pace_checker(pace) if flag else ""
    return jsonify({'Pace_Checker': output})


def get_Chat_response(text):
    global flag
    global prev_q

    role = position[0] if len(position) > 0 else "Software Developer"

    # Build resume context if available (fetch from database)
    resume_context = ""
    resume_data = None
    
    if 'session_id' in session:
        db = get_db()
        try:
            resume_record = db.query(Resume).filter_by(session_id=session['session_id']).first()
            if resume_record and resume_record.extracted_json:
                resume_data = json.loads(resume_record.extracted_json)
        except Exception as e:
            print(f"Error fetching resume from database: {e}")
        finally:
            db.close()
    
    if resume_data:
        skills = resume_data.get('skills', [])
        experience = resume_data.get('experience', [])
        education = resume_data.get('college_name', [])
        if skills:
            resume_context += f"Candidate Skills: {', '.join(skills[:10])}. "
        if experience:
            resume_context += f"Experience: {experience[0] if experience else 'N/A'}. "
        if education:
            resume_context += f"Education: {education[0] if education else 'N/A'}."

    if flag == 0:
        prompt = f"""
You are a strict technical interviewer.

Role: {role}

{resume_context}

Question:
Tell me about yourself.

Candidate Answer:
{text}

Generate ONE follow-up interview question related to the answer and the candidate's resume.

Rules:
- Ask only ONE question.
- Do not explain.
- Do not repeat previous questions.
- Keep it under 25 words.
- Use the resume context to personalize the question.
"""
    else:
        previous_question = prev_q[-1] if len(prev_q) > 0 else "Tell me about yourself"

        prompt = f"""
You are a strict technical interviewer.

Role: {role}

{resume_context}

Previous Question:
{previous_question}

Candidate Answer:
{text}

Generate ONE NEW follow-up interview question.

Rules:
- Ask only ONE question.
- Do not explain.
- Do not repeat previous questions.
- Keep it under 25 words.
- Use the resume context to personalize the question.
"""

    flag += 1

    output = generate_output(prompt)

    feed = grammar_checker(text)
    feedback.append(feed)

    output = output.strip()

    if len(output) > 200:
        output = output[:200]

    prev_q.append(output)

    return output

def get_Chat_response_stream(text):
    """Streaming version of chat response with AI scoring"""
    global flag
    global prev_q

    # Check for quit intent
    quit_keywords = ['quit', 'exit', 'stop', 'end interview', 'finish']
    if any(keyword in text.lower() for keyword in quit_keywords):
        yield "INTERVIEW_END"
        return

    role = position[0] if len(position) > 0 else "Software Developer"

    # Build resume context
    resume_context = ""
    resume_data = None
    
    if 'session_id' in session:
        db = get_db()
        try:
            resume_record = db.query(Resume).filter_by(session_id=session['session_id']).first()
            if resume_record and resume_record.extracted_json:
                resume_data = json.loads(resume_record.extracted_json)
        except Exception as e:
            print(f"Error fetching resume from database: {e}")
        finally:
            db.close()
    
    if resume_data:
        skills = resume_data.get('skills', [])
        experience = resume_data.get('experience', [])
        education = resume_data.get('college_name', [])
        if skills:
            resume_context += f"Candidate Skills: {', '.join(skills[:10])}. "
        if experience:
            resume_context += f"Experience: {experience[0] if experience else 'N/A'}. "
        if education:
            resume_context += f"Education: {education[0] if education else 'N/A'}."

    if flag == 0:
        prompt = f"""
You are a strict technical interviewer.

Role: {role}

{resume_context}

Question:
Tell me about yourself.

Candidate Answer:
{text}

Generate ONE follow-up interview question related to the answer and the candidate's resume.

Rules:
- Ask only ONE question.
- Do not explain.
- Do not repeat previous questions.
- Keep it under 25 words.
- Use the resume context to personalize the question.
"""
    else:
        previous_question = prev_q[-1] if len(prev_q) > 0 else "Tell me about yourself"

        prompt = f"""
You are a strict technical interviewer.

Role: {role}

{resume_context}

Previous Question:
{previous_question}

Candidate Answer:
{text}

Generate ONE NEW follow-up interview question.

Rules:
- Ask only ONE question.
- Do not explain.
- Do not repeat previous questions.
- Keep it under 25 words.
- Use the resume context to personalize the question.
"""

    flag += 1

    # Stream the response
    full_response = ""
    for chunk in generate_output_stream(prompt):
        full_response += chunk
        yield chunk
    
    full_response = full_response.strip()
    if len(full_response) > 200:
        full_response = full_response[:200]
    
    prev_q.append(full_response)

    # Generate AI score for the answer
    score_prompt = f"""
You are an experienced interviewer evaluating a candidate's answer.

Position: {role}
Question: {previous_question if flag > 1 else "Tell me about yourself"}
Candidate Answer: {text}

Evaluate the answer and return ONLY a JSON object with this exact format:
{{"technical_accuracy": X, "communication": Y, "problem_solving": Z, "feedback": "..."}}

Where X, Y, Z are integers from 1-10 and feedback is a brief explanation.
"""

    try:
        score_response = generate_score(score_prompt)
        # Parse JSON from response
        import re
        json_match = re.search(r'\{.*\}', score_response, re.DOTALL)
        if json_match:
            score_data = json.loads(json_match.group())
            
            # Save score to database
            if 'interview_session_id' in session:
                db = get_db()
                try:
                    ai_score = AIScore(
                        session_id=session['interview_session_id'],
                        question=previous_question if flag > 1 else "Tell me about yourself",
                        answer=text,
                        technical_accuracy=score_data.get('technical_accuracy', 5),
                        communication=score_data.get('communication', 5),
                        problem_solving=score_data.get('problem_solving', 5),
                        feedback=score_data.get('feedback', '')
                    )
                    db.add(ai_score)
                    db.commit()
                except Exception as e:
                    db.rollback()
                    print(f"Error saving score: {e}")
                finally:
                    db.close()
    except Exception as e:
        print(f"Error generating score: {e}")

    # Grammar check (legacy)
    feed = grammar_checker(text)
    feedback.append(feed)

def calculate_speaking_pace(transcription, chunk_length):
    words = transcription.split()
    num_words = len(words)
    speaking_rate = num_words / chunk_length  # Words per second
    return speaking_rate

def pace_checker(pace):
    optimal_pace_range = (1, 3)
    if optimal_pace_range[0] <= pace <= optimal_pace_range[1]:
        return "Good Pace"
    elif pace < optimal_pace_range[0]:
        return "Very Slow"
    elif pace > optimal_pace_range[1]:
        return "Too Fast"


def grammar_checker(text):
    input = f"""
    Correct “{text}” to standard English and place the results in “Correct Text:”
"""
    output = generate_output(input)
    return output.split(':')[-1]

def answer_checker(text,question):
    # input = f""" Question : {question}
    #             Candidate answer : {text}
    #             Considering the answer for the question, output only 'YES' if answer is correct or else output only 'NO'.
    #         output:
    #  """
    input = f"""### instruction: you are an experienced interviewer.\
   You are interviewing a candidate for the position of {position[0]} .\
   You are tasked to rate an answer provided by the candidate. You should provide a categorical rating and qualitative feedback.\
    The categorical rating should be one of the following values: Good, average, or  Poor.\
      the qualitative feedback should provide sufficient details to justify the categorical rating.\
        the format instructions of the output and the question asked to the candidate and the answer given by the candidate are  given below.\
        "" I  want rating between 1 to 10 only please. 10 showing a perfect answer for the given question. Give rating only only""
        ### question:{question}.\
        ### answer:{text}.\
        ### Rating:
        """
    output = generate_output(input)
    output = output.strip()
    return output

if __name__ == '__main__':
    app.run()