from concurrent.futures import ThreadPoolExecutor
import os
import json
import re
import uuid
import zipfile

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response, stream_with_context
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import Config
from model import generate_output, generate_output_stream, generate_score
from faster_whisper import WhisperModel
from resume_parser import parse_resume, FileValidationError, ResumeParsingError
from database import (
    init_db, get_db, Resume, Candidate, InterviewSession, AIScore, User, Job,
    JobApplication, CandidateEvaluation,
)
from scoring_engine import calculate_composite_score

model_audio = WhisperModel(model_size_or_path="small")

app = Flask(__name__)
app.config.from_object(Config)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
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
        # Use Session.get() (SQLAlchemy 2.0 compatible) instead of legacy Query.get()
        return db.get(User, int(user_id))
    finally:
        db.close()


# File upload validation
ALLOWED_EXTENSIONS = {'pdf', 'docx'}
ALLOWED_MIME_TYPES = {
    'pdf': ['application/pdf'],
    'docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
}


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def validate_mime_type(file, filename):
    """Validate file MIME type."""
    ext = filename.rsplit('.', 1)[1].lower()
    file.seek(0)
    header = file.read(2048)
    file.seek(0)

    if ext == 'pdf':
        return header.startswith(b'%PDF')

    if ext == 'docx':
        return header.startswith(b'PK')

    return False


def _init_session_state():
    if 'interview_state' not in session:
        session['interview_state'] = {
            'name': '',
            'position': 'Software Developer',
            'prev_q': [],
            'flag': 0,
            'feedback': ['Grammatical correction here'],
            'pace': 0.0,
        }
    return session['interview_state']


def get_interview_state():
    return _init_session_state()


def _fallback_resume_parser(text, filename='resume'):
    data = {'name': '', 'email': '', 'mobile_number': '', 'skills': [], 'college_name': [], 'total_experience': ''}
    if not text:
        return data

    match_email = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', text)
    if match_email:
        data['email'] = match_email.group(0)

    match_phone = re.search(r'(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})', text)
    if match_phone:
        data['mobile_number'] = re.sub(r'\s+', '', match_phone.group(0))

    match_name = re.search(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text)
    if match_name:
        data['name'] = match_name.group(1)

    skill_keywords = [
        'Python', 'Java', 'JavaScript', 'TypeScript', 'Flask', 'Django', 'React', 'Node', 'SQL', 'PostgreSQL',
        'MySQL', 'MongoDB', 'AWS', 'Docker', 'Kubernetes', 'Azure', 'Git', 'Linux', 'Machine Learning', 'AI',
        'NLP', 'C++', 'C#', 'Data Analysis', 'Power BI', 'Tableau', 'REST API', 'API', 'HTML', 'CSS'
    ]
    found_skills = []
    for skill in skill_keywords:
        if re.search(rf'\b{re.escape(skill)}\b', text, re.IGNORECASE):
            found_skills.append(skill)
    data['skills'] = found_skills[:10]

    college_matches = re.findall(r'([A-Z][A-Za-z0-9&.\- ]+(?:University|Institute|College|School))', text)
    if college_matches:
        data['college_name'] = list(dict.fromkeys(college_matches))[:3]

    experience_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:of\s*)?experience', text, re.IGNORECASE)
    if not experience_match:
        experience_match = re.search(r'(\d+)\s*(?:years?|yrs?)', text, re.IGNORECASE)
    if experience_match:
        data['total_experience'] = experience_match.group(1)

    if not data['name'] and filename:
        base_name = os.path.splitext(os.path.basename(filename))[0]
        sanitized = re.sub(r'[_-]+', ' ', base_name)
        if sanitized and 'resume' not in sanitized.lower():
            data['name'] = sanitized.title()

    return data


def _safe_gemini_call(prompt, fallback_parser=None, timeout_seconds=15):
    if not os.getenv('GEMINI_API_KEY'):
        if callable(fallback_parser):
            return fallback_parser(prompt)
        return ''

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(generate_output, prompt)
            return future.result(timeout=timeout_seconds)
    except Exception:
        if callable(fallback_parser):
            return fallback_parser(prompt)
        return ''


def _fallback_score_parser(prompt):
    text = prompt or ''
    technical = re.search(r'technical_accuracy\s*[:=]\s*(\d)', text, re.IGNORECASE)
    communication = re.search(r'communication\s*[:=]\s*(\d)', text, re.IGNORECASE)
    problem = re.search(r'problem_solving\s*[:=]\s*(\d)', text, re.IGNORECASE)
    feedback = 'Response captured with rule-based fallback evaluation.'
    return {
        'technical_accuracy': int(technical.group(1)) if technical else 6,
        'communication': int(communication.group(1)) if communication else 6,
        'problem_solving': int(problem.group(1)) if problem else 6,
        'feedback': feedback,
    }


def _fallback_grammar_parser(text):
    if not text:
        return 'No text provided'
    cleaned = re.sub(r'\s+', ' ', text).strip()
    return cleaned if cleaned else 'No text provided'

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
        weight_resume = request.form.get("weight_resume", weight_experience)
        weight_ai = request.form.get("weight_ai", weight_education)
        cutoff_score = request.form.get("cutoff_score", 75)
        scenario_prompt = request.form.get("scenario_prompt", "")
        
        if not title or not company:
            return render_template('job_form.html', error="Title and company are required")
        
        # Validate weightage sums to 100
        total_weight = float(weight_skills) + float(weight_resume) + float(weight_ai)
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
                weight_experience=int(float(weight_resume)),
                weight_education=int(float(weight_ai)),
                weight_resume=float(weight_resume),
                weight_ai=float(weight_ai),
                cutoff_score=float(cutoff_score),
                scenario_prompt=scenario_prompt,
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
            job.weight_skills = float(request.form.get("weight_skills", 40))
            job.weight_resume = float(request.form.get("weight_resume", request.form.get("weight_experience", 30)))
            job.weight_ai = float(request.form.get("weight_ai", request.form.get("weight_education", 30)))
            job.weight_experience = int(job.weight_resume)
            job.weight_education = int(job.weight_ai)
            job.cutoff_score = float(request.form.get("cutoff_score", 75))
            job.scenario_prompt = request.form.get("scenario_prompt", "")
            
            # Validate weightage
            total_weight = job.weight_skills + job.weight_resume + job.weight_ai
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
    if current_user.role not in ['admin', 'recruiter']:
        return redirect(url_for('index'))
    db = get_db()
    try:
        applications = db.query(JobApplication).order_by(JobApplication.applied_at.desc()).all()
        candidates_data = []
        for application in applications:
            evaluation = calculate_composite_score(db, application.id)
            candidate = db.get(Candidate, application.candidate_id)
            job = db.get(Job, application.job_id)
            latest_session = (
                db.query(InterviewSession)
                .filter_by(candidate_id=candidate.id)
                .order_by(InterviewSession.created_at.desc())
                .first()
            )
            candidates_data.append({
                'id': candidate.id,
                'application_id': application.id,
                'name': candidate.name,
                'email': candidate.email,
                'resume_filename': candidate.resume_filename,
                'position': job.title if job else 'N/A',
                'status': evaluation.status,
                'resume_score': round(evaluation.resume_score, 1),
                'skills_score': round(evaluation.skills_score, 1),
                'ai_response_score': round(evaluation.ai_response_score, 1),
                'final_weighted_score': round(evaluation.final_weighted_score, 1),
                'session_id': latest_session.id if latest_session else None,
                'created_at': application.applied_at.strftime('%Y-%m-%d %H:%M'),
            })
        candidates_data.sort(key=lambda item: item['final_weighted_score'], reverse=True)
    except Exception as e:
        print(f"Error fetching admin data: {e}")
        candidates_data = []
    finally:
        db.close()
    
    return render_template('admin.html', candidates=candidates_data)


@app.route("/admin/export-shortlist")
@login_required
def export_shortlist():
    """Export candidates meeting their job's configured cutoff."""
    if current_user.role not in ['admin', 'recruiter']:
        return redirect(url_for('index'))
    import csv
    from io import StringIO

    db = get_db()
    try:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Candidate', 'Email', 'Job', 'Resume Score', 'Skills Score', 'AI Score', 'Final Score', 'Status'])
        applications = db.query(JobApplication).order_by(JobApplication.applied_at.desc()).all()
        for application in applications:
            evaluation = calculate_composite_score(db, application.id)
            job = db.get(Job, application.job_id)
            candidate = db.get(Candidate, application.candidate_id)
            if evaluation.final_weighted_score >= float(getattr(job, 'cutoff_score', 75.0) or 75.0):
                writer.writerow([
                    candidate.name, candidate.email or '', job.title if job else '',
                    f'{evaluation.resume_score:.1f}', f'{evaluation.skills_score:.1f}',
                    f'{evaluation.ai_response_score:.1f}', f'{evaluation.final_weighted_score:.1f}',
                    evaluation.status,
                ])
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=shortlist.csv'},
        )
    finally:
        db.close()

@app.route("/admin/candidate/<int:session_id>")
@login_required
def candidate_details(session_id):
    """Get detailed candidate information and scores"""
    if current_user.role not in ['admin', 'recruiter']:
        return jsonify({'error': 'Forbidden'}), 403
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
        session_state = get_interview_state()
        session_state['name'] = username
        session_state['position'] = pos
        
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
                   return render_template('home.html', name=session_state.get('name', ''), error="Invalid file type. Only PDF and DOCX files are allowed.")

                # Validate MIME type (magic bytes check)
                if not validate_mime_type(resume_file, resume_file.filename):
                   return render_template('home.html', name=session_state.get('name', ''), error="File content does not match the file extension. Please upload a valid file.")
                
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
                        resume_fallback = _fallback_resume_parser(open(filepath, 'r', encoding='utf-8', errors='ignore').read() if filepath.lower().endswith('.txt') else '')
                        if resume_fallback.get('email') or resume_fallback.get('mobile_number'):
                            parsed_data = {**parsed_data, **resume_fallback}
                        else:
                            return render_template('home.html', name=session_state.get('name', ''), error="Resume must contain at least an email or phone number.")
                    
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
                        return render_template('home.html', name=session_state.get('name', ''), error="Failed to save resume data to database.")
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
                    return render_template('home.html', name=session_state.get('name', ''), error=f"Resume validation failed: {str(e)}")
                except ResumeParsingError as e:
                    print(f"Resume parsing error: {e}")
                    # Build a lightweight regex-based fallback from the uploaded file text before cleanup
                    try:
                        with open(filepath, 'rb') as fh:
                            raw = fh.read()
                        text = raw.decode('utf-8', errors='ignore') if filepath.lower().endswith('.txt') else ''
                        parsed_data = _fallback_resume_parser(text, filepath)
                    except Exception:
                        parsed_data = _fallback_resume_parser('', filepath)
                    if parsed_data.get('email') or parsed_data.get('mobile_number'):
                        return render_template('home.html', name=session_state.get('name', ''), resume_data=parsed_data)
                    # Clean up uploaded file
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return render_template('home.html', name=session_state.get('name', ''), error=f"Resume parsing failed: {str(e)}. Please ensure the file is not corrupted.")
                except Exception as e:
                    print(f"Unexpected error during resume processing: {e}")
                    # Clean up uploaded file
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return render_template('home.html', name=session_state.get('name', ''), error=f"An error occurred while processing your resume. Please try again.")
        
        return render_template('home.html', name=session_state.get('name', ''), resume_data=parsed_data)
    return render_template('home.html', name=get_interview_state().get('name', ''))

@app.route("/get_flag", methods=["GET"])
def get_flag():
    feedback = get_interview_state().get('feedback', [])
    if len(feedback) > 0:
        return jsonify({'flag': feedback[-1]})
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
    interview_state = get_interview_state()
    audio_path = "./audio.wav"
    result = model_audio.transcribe(audio_path)
    segments, info = result
    print("Detected language '%s' with probability %f" % (info.language, info.language_probability))
    text = ""
    for segment in segments:
        text += segment.text
    interview_state['pace'] = calculate_speaking_pace(text, chunk_length=DEFAULT_CHUNK_LENGTH)
    return text

@app.route('/get_pace', methods=['GET'])
def get_pace():
    interview_state = get_interview_state()
    output = pace_checker(interview_state.get('pace', 0.0)) if interview_state.get('flag', 0) else ""
    return jsonify({'Pace_Checker': output})


def get_Chat_response(text):
    interview_state = get_interview_state()
    role = interview_state.get('position', 'Software Developer')
    flag = int(interview_state.get('flag', 0))
    prev_q = interview_state.get('prev_q', [])

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

    previous_question = prev_q[-1] if len(prev_q) > 0 else "Tell me about yourself"

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

    interview_state['flag'] = flag + 1

    def fallback_prompt_response(_prompt):
        if 'Tell me about yourself' in _prompt:
            return 'Can you walk me through a project where you solved a real technical problem?'
        return 'Can you give an example of how you applied your strongest skill to a business problem?'

    output = _safe_gemini_call(prompt, fallback_parser=fallback_prompt_response, timeout_seconds=15)
    feed = grammar_checker(text)
    interview_state.setdefault('feedback', []).append(feed)

    output = str(output).strip() or fallback_prompt_response(prompt)

    if len(output) > 200:
        output = output[:200]

    prev_q.append(output)
    interview_state['prev_q'] = prev_q

    return output

def get_Chat_response_stream(text):
    """Streaming version of chat response with AI scoring"""
    interview_state = get_interview_state()
    flag = int(interview_state.get('flag', 0))
    prev_q = interview_state.get('prev_q', [])

    # Check for quit intent
    quit_keywords = ['quit', 'exit', 'stop', 'end interview', 'finish']
    if any(keyword in text.lower() for keyword in quit_keywords):
        yield "INTERVIEW_END"
        return

    role = interview_state.get('position', 'Software Developer')

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

    previous_question = prev_q[-1] if len(prev_q) > 0 else "Tell me about yourself"

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

    interview_state['flag'] = flag + 1

    # Stream the response
    full_response = ""
    try:
        for chunk in generate_output_stream(prompt):
            full_response += str(chunk)
            yield chunk
    except Exception:
        fallback = "AI service unavailable. Please verify the Gemini API key in the project .env file."
        yield fallback
        full_response = fallback

    full_response = str(full_response).strip()
    if len(full_response) > 200:
        full_response = full_response[:200]

    prev_q.append(full_response)
    interview_state['prev_q'] = prev_q

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
        score_response = _safe_gemini_call(score_prompt, fallback_parser=lambda prompt: _fallback_score_parser(prompt), timeout_seconds=15)
        json_match = re.search(r'\{.*\}', str(score_response), re.DOTALL)
        if json_match:
            score_data = json.loads(json_match.group())

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

    feed = grammar_checker(text)
    interview_state.setdefault('feedback', []).append(feed)

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
   prompt = f"""
   Correct “{text}” to standard English and place the results in “Correct Text:”
   """
   output = _safe_gemini_call(prompt, fallback_parser=lambda _: _fallback_grammar_parser(text), timeout_seconds=15)
   if not output:
       return _fallback_grammar_parser(text)
   return str(output).split(':')[-1]


def answer_checker(text, question):
   interview_state = get_interview_state()
   role = interview_state.get('position', 'Software Developer')
   prompt = f"""### instruction: you are an experienced interviewer.\
   You are interviewing a candidate for the position of {role}.\
   You are tasked to rate an answer provided by the candidate. You should provide a categorical rating and qualitative feedback.\
    The categorical rating should be one of the following values: Good, average, or  Poor.\
      the qualitative feedback should provide sufficient details to justify the categorical rating.\
        the format instructions of the output and the question asked to the candidate and the answer given by the candidate are  given below.\
        "" I want rating between 1 to 10 only please. 10 showing a perfect answer for the given question. Give rating only only""
        ### question:{question}.\
        ### answer:{text}.\
        ### Rating:
        """
   output = _safe_gemini_call(prompt, fallback_parser=lambda _: f"Good - {text[:120]}", timeout_seconds=15)
   return str(output).strip() or f"Good - {text[:120]}"
if __name__ == '__main__':
    app.run()