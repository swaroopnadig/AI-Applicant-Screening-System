from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response, stream_with_context
import click
import csv
import io
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from model import generate_output, generate_output_stream, generate_score
from faster_whisper import WhisperModel
import os
import json
import uuid
import smtplib
import re
import secrets
import time
from datetime import datetime
from hmac import compare_digest
from urllib.parse import urljoin, urlparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from resume_parser import parse_resume, FileValidationError, ResumeParsingError
from database import (
    init_db, get_db, Resume, Candidate, InterviewSession, AIScore, User, Job,
    JobApplication, AssessmentQuestion, AssessmentAttempt, CodingProblem,
    CodingSubmission,
)
from scoring import (
    ROUND_NAMES, calculate_final_score, default_rounds, evaluate_eligibility,
    parse_rounds, score_resume, validate_rounds,
)

model_audio = None


def get_audio_model():
    global model_audio
    if model_audio is None:
        model_audio = WhisperModel(model_size_or_path="small")
    return model_audio

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', 'uploads')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max file size
# Security configuration
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'False').lower() in ['true', '1']
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Email configuration
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', '587'))
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD', '')
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() in ['true', '1']
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', 'noreply@aiinterview.com')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


@app.context_processor
def inject_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["_csrf_token"]}


@app.before_request
def protect_unsafe_requests():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    expected = session.get("_csrf_token")
    provided = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not provided or not compare_digest(expected, provided):
        return "The form security token is invalid or expired. Reload and retry.", 400
    return None


# Initialize database
init_db()

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'


@app.cli.command("create-admin")
@click.option("--username", prompt=True)
@click.option("--email", prompt=True)
@click.password_option(confirmation_prompt=True)
def create_admin_command(username, email, password):
    """Create an administrator account from a trusted command line."""
    username = sanitize_input(username)
    email = sanitize_input(email).lower()
    valid_password, password_message = validate_password_strength(password)
    if not username or not re.match(r"^[a-zA-Z0-9_]+$", username):
        raise click.ClickException(
            "Username must contain only letters, numbers, and underscores."
        )
    if not validate_email(email):
        raise click.ClickException("Enter a valid email address.")
    if not valid_password:
        raise click.ClickException(password_message)

    db = get_db()
    try:
        if db.query(User).filter(
            (User.username == username) | (User.email == email)
        ).first():
            raise click.ClickException("That username or email is already registered.")
        db.add(User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            role="admin",
        ))
        db.commit()
    except click.ClickException:
        db.rollback()
        raise
    except Exception as error:
        db.rollback()
        app.logger.exception("Failed to create administrator account")
        raise click.ClickException("Unable to create administrator account.") from error
    finally:
        db.close()
    click.echo("Administrator account created.")


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    try:
        return db.query(User).get(int(user_id))
    finally:
        db.close()


def recruiter_required(f):
    """Restrict a route to logged-in admin or recruiter users."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if current_user.role not in ('admin', 'recruiter'):
            wants_json = (
                request.path.startswith('/admin/candidate')
                or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            )
            if wants_json:
                return jsonify({'error': 'Unauthorized'}), 403
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


def candidate_required(f):
    """Restrict a route to authenticated candidate accounts."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if current_user.role != "candidate":
            return redirect(url_for("admin"))
        return f(*args, **kwargs)
    return decorated_function


def candidate_interview_required(f):
    """Require an interview session that belongs to the signed-in candidate."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if current_user.role != "candidate":
            return "Candidate access required.", 403
        interview_session_id = session.get("interview_session_id")
        if not interview_session_id:
            return "No active interview session.", 403

        db = get_db()
        try:
            candidate = db.query(Candidate).filter_by(
                name=current_user.username
            ).first()
            interview_session = db.query(InterviewSession).filter_by(
                id=interview_session_id
            ).first()
            if (
                not candidate
                or not interview_session
                or interview_session.candidate_id != candidate.id
            ):
                return "No active interview session.", 403
        finally:
            db.close()
        return f(*args, **kwargs)
    return decorated_function


def start_interview_browser_session(username, pos):
    """Store interview state on this browser session (not process globals)."""
    session['session_id'] = os.urandom(16).hex()
    session['candidate_name'] = username
    session['interview_position'] = pos
    session['prev_q'] = []
    session['interview_flag'] = 0
    session['grammar_feedback'] = 'Grammatical correction here'
    session['speaking_pace'] = 0.0
    session.modified = True
    return session['session_id']


def interview_audio_path():
    """Per-browser audio file so two candidates cannot overwrite each other."""
    if 'session_id' not in session:
        session['session_id'] = os.urandom(16).hex()
    token = ''.join(c for c in session['session_id'] if c.isalnum())[:32] or 'anon'
    return os.path.join(app.config['UPLOAD_FOLDER'], f'audio_{token}.wav')


def get_or_create_candidate(db, username, email=''):
    candidate = db.query(Candidate).filter_by(name=username).first()
    if candidate:
        if email and not candidate.email:
            candidate.email = email
        return candidate
    candidate = Candidate(
        name=username,
        email=email or '',
        resume_filename='',
        parsed_text='',
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return candidate


def send_email_notification(recipient, subject, body):
    """Send email notification using SMTP"""
    if not recipient or not app.config['MAIL_USERNAME'] or not app.config['MAIL_PASSWORD']:
        print("Email notification skipped: missing recipient or credentials")
        return False
    
    try:
        msg = MIMEMultipart()
        msg['From'] = app.config['MAIL_DEFAULT_SENDER']
        msg['To'] = recipient
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'html'))
        
        server = smtplib.SMTP(app.config['MAIL_SERVER'], app.config['MAIL_PORT'])
        if app.config['MAIL_USE_TLS']:
            server.starttls()
        server.login(app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'])
        server.send_message(msg)
        server.quit()
        
        print(f"Email sent successfully to {recipient}")
        return True
    except Exception as e:
        print(f"Failed to send email to {recipient}: {e}")
        return False


# File upload validation
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}
ALLOWED_MIME_TYPES = {
    'pdf': ['application/pdf'],
    'docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    'txt': ['text/plain']
}

APPLICATION_STATUSES = (
    "Applied",
    "Under Review",
    "Shortlisted",
    "Selected",
    "Rejected",
)


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
    
    # DOCX files are ZIP packages, so reject files without a ZIP signature.
    if ext == 'docx':
        return header.startswith(b'PK\x03\x04')
    
    return False

def validate_email(email):
    """Validate email format using regex"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_password_strength(password):
    """Validate password strength (min 8 chars, at least 1 uppercase, 1 lowercase, 1 number)"""
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r'\d', password):
        return False, "Password must contain at least one number"
    return True, "Password is valid"


def is_safe_redirect_url(target):
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return (
        redirect_url.scheme in ("http", "https")
        and redirect_url.netloc == host_url.netloc
    )


def sanitize_input(input_string):
    """Basic input sanitization to prevent XSS"""
    if not input_string:
        return ""
    # Remove script tags and basic HTML
    sanitized = re.sub(r'<script.*?>.*?</script>', '', input_string, flags=re.IGNORECASE | re.DOTALL)
    sanitized = re.sub(r'<[^>]+>', '', sanitized)
    return sanitized.strip()


def parse_job_form(form):
    """Validate job details and recruitment-round configuration from an admin form."""
    title = (form.get("title") or "").strip()
    company = (form.get("company") or "").strip()
    if not title or not company:
        raise ValueError("Title and company are required.")

    try:
        minimum_experience = int(form.get("minimum_experience") or 0)
        minimum_cgpa = form.get("minimum_cgpa", "").strip()
        minimum_cgpa = float(minimum_cgpa) if minimum_cgpa else None
        aptitude_time_limit = int(form.get("aptitude_time_limit") or 30)
        technical_time_limit = int(form.get("technical_time_limit") or 30)
    except (TypeError, ValueError):
        raise ValueError("Experience, CGPA, and time limit values must be numeric.")

    if minimum_experience < 0:
        raise ValueError("Minimum experience cannot be negative.")
    if minimum_cgpa is not None and not 0 <= minimum_cgpa <= 100:
        raise ValueError("Minimum CGPA/percentage must be between 0 and 100.")
    if not 1 <= aptitude_time_limit <= 240 or not 1 <= technical_time_limit <= 240:
        raise ValueError("Assessment time limits must be between 1 and 240 minutes.")

    rounds = {}
    for name in ROUND_NAMES:
        try:
            weight = int(form.get("weight_{}".format(name)) or 0)
        except (TypeError, ValueError):
            raise ValueError("{} weight must be a whole number.".format(ROUND_NAMES[name]))
        enabled = form.get("round_{}".format(name)) == "on"
        rounds[name] = {
            "enabled": enabled,
            "weight": weight if enabled else 0,
        }
    validate_rounds(rounds)

    skill_list = [
        skill.strip()
        for skill in (form.get("required_skills") or "").split(",")
        if skill.strip()
    ]
    return {
        "title": title,
        "company": company,
        "location": (form.get("location") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "required_skills": json.dumps(skill_list),
        "minimum_experience": minimum_experience,
        "minimum_education": form.get("minimum_education") or None,
        "minimum_cgpa": minimum_cgpa,
        "eligibility_enabled": form.get("eligibility_enabled") == "on",
        "rounds_json": json.dumps(rounds),
        "aptitude_time_limit": aptitude_time_limit,
        "technical_time_limit": technical_time_limit,
    }


def refresh_application_score(db, application):
    """Recompute the final score from enabled rounds with an available score."""
    job = db.query(Job).filter_by(id=application.job_id).first()
    if not job:
        return
    rounds = parse_rounds(job.rounds_json)
    candidate = db.query(Candidate).filter_by(id=application.candidate_id).first()
    if candidate and (candidate.resume_filename or candidate.parsed_text):
        application.resume_score = score_resume(candidate, job)
    else:
        application.resume_score = None
    scores = {
        "resume_screening": application.resume_score,
        "aptitude": None,
        "technical": application.technical_score,
        "coding": application.coding_score,
        "ai_interview": application.interview_score,
    }
    attempts = db.query(AssessmentAttempt).filter_by(
        application_id=application.id, round_type="aptitude"
    ).first()
    if attempts:
        scores["aptitude"] = attempts.score
    application.final_score = calculate_final_score(rounds, scores)


def refresh_interview_application_scores(db, interview_session_id):
    interview_session = db.query(InterviewSession).filter_by(
        id=interview_session_id
    ).first()
    if not interview_session:
        return
    interview_scores = db.query(AIScore).filter_by(
        session_id=interview_session_id
    ).all()
    if not interview_scores:
        return
    interview_score = round(
        sum(max(0, min(10, score.weighted_score)) for score in interview_scores)
        * 10.0 / len(interview_scores),
        2,
    )
    applications = db.query(JobApplication).filter_by(
        candidate_id=interview_session.candidate_id
    ).all()
    for application in applications:
        job = db.query(Job).filter_by(id=application.job_id).first()
        if job and job.title.strip().lower() == (interview_session.position or "").strip().lower():
            application.interview_score = interview_score
            refresh_application_score(db, application)


@app.route("/", methods=["GET", "POST"])
def index():
    return render_template('index.html')

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")
        role = "candidate"
        
        # Sanitize inputs
        username = sanitize_input(username)
        email = sanitize_input(email)
        
        # Basic validation
        if not username or not email or not password:
            return render_template('register.html', error="All fields are required")
        
        # Email validation
        if not validate_email(email):
            return render_template('register.html', error="Invalid email format")
        
        # Password strength validation
        is_valid, password_msg = validate_password_strength(password)
        if not is_valid:
            return render_template('register.html', error=password_msg)
        
        # Username validation (alphanumeric and underscore only)
        if not re.match(r'^[a-zA-Z0-9_]+$', username):
            return render_template('register.html', error="Username can only contain letters, numbers, and underscores")
        
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
            if role == 'candidate':
                get_or_create_candidate(db, username, email)
            
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
                session["_csrf_token"] = secrets.token_urlsafe(32)
                next_page = request.args.get('next')
                safe_next_page = next_page if is_safe_redirect_url(next_page) else None
                if user.role in ['admin', 'recruiter']:
                    return redirect(safe_next_page or url_for('admin'))
                else:
                    return redirect(safe_next_page or url_for('candidate_dashboard'))
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
        for job in jobs:
            job.required_skills_list = json.loads(job.required_skills or "[]")
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
        job.rounds = parse_rounds(job.rounds_json)
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
        
        candidate = get_or_create_candidate(
            db, current_user.username, current_user.email or ''
        )
        
        # Check if already applied
        existing = db.query(JobApplication).filter_by(job_id=job_id, candidate_id=candidate.id).first()
        if existing:
            return redirect(url_for('job_details', job_id=job_id))
        
        eligibility = evaluate_eligibility(candidate, job)
        application = JobApplication(
            job_id=job_id,
            candidate_id=candidate.id,
            status=(
                "Rejected" if job.eligibility_enabled and not eligibility["eligible"]
                else "Under Review" if job.eligibility_enabled
                else "Applied"
            ),
            eligibility_result=eligibility["result"],
            eligibility_details=json.dumps(eligibility["details"]),
            resume_score=eligibility["resume_score"],
        )
        db.add(application)
        db.commit()
        
        return redirect(url_for('job_details', job_id=job_id))
    finally:
        db.close()


@app.route("/candidate/dashboard")
@login_required
def candidate_dashboard():
    if current_user.role != "candidate":
        return redirect(url_for("admin"))
    db = get_db()
    try:
        candidate = db.query(Candidate).filter_by(name=current_user.username).first()
        if candidate:
            candidate.resume_data = {}
            if candidate.parsed_text:
                try:
                    resume_data = json.loads(candidate.parsed_text)
                    if isinstance(resume_data, dict):
                        for field in (
                            "skills", "education", "college_name",
                            "projects", "experience",
                        ):
                            value = resume_data.get(field)
                            if isinstance(value, str):
                                resume_data[field] = [value] if value else []
                            elif not isinstance(value, list):
                                resume_data[field] = []
                        candidate.resume_data = resume_data
                except (TypeError, ValueError):
                    app.logger.warning(
                        "Unable to read parsed resume data for candidate %s",
                        candidate.id,
                    )
        applications = []
        if candidate:
            records = db.query(JobApplication).filter_by(
                candidate_id=candidate.id
            ).order_by(JobApplication.applied_at.desc()).all()
            for application in records:
                job = db.query(Job).filter_by(id=application.job_id).first()
                if not job:
                    continue
                try:
                    application.eligibility_reasons = json.loads(
                        application.eligibility_details or "[]"
                    )
                except (TypeError, ValueError):
                    application.eligibility_reasons = []
                rounds = parse_rounds(job.rounds_json)
                attempts = {
                    attempt.round_type: attempt
                    for attempt in db.query(AssessmentAttempt).filter_by(
                        application_id=application.id
                    ).all()
                }
                refresh_application_score(db, application)
                applications.append({
                    "application": application,
                    "job": job,
                    "rounds": rounds,
                    "attempts": attempts,
                    "coding_submissions": db.query(CodingSubmission).filter_by(
                        application_id=application.id
                    ).order_by(CodingSubmission.submitted_at.desc()).all(),
                })
            db.commit()
        return render_template(
            "candidate_dashboard.html", candidate=candidate,
            applications=applications,
        )
    finally:
        db.close()


@app.route("/admin/jobs/<int:job_id>/questions/<round_type>", methods=["GET", "POST"])
@recruiter_required
def manage_assessment_questions(job_id, round_type):
    if round_type not in ("aptitude", "technical"):
        return "Unsupported assessment round.", 404
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return redirect(url_for("admin_jobs"))
        error = None
        if request.method == "POST":
            question_text = (request.form.get("question") or "").strip()
            category = (request.form.get("category") or "").strip()
            options = [
                option.strip()
                for option in (request.form.get("options") or "").splitlines()
                if option.strip()
            ]
            try:
                correct_option = int(request.form.get("correct_option", "-1"))
                marks = float(request.form.get("marks") or 1)
            except ValueError:
                correct_option, marks = -1, 0
            if not question_text or not category:
                error = "Question and category are required."
            elif len(options) < 2 or len(options) > 6:
                error = "Provide between 2 and 6 answer options, one per line."
            elif len({option.casefold() for option in options}) != len(options):
                error = "Answer options must be unique."
            elif correct_option < 0 or correct_option >= len(options):
                error = "Choose a correct answer that matches one of the options."
            elif marks <= 0 or marks > 100:
                error = "Marks must be greater than 0 and no more than 100."
            else:
                db.add(AssessmentQuestion(
                    job_id=job.id,
                    round_type=round_type,
                    category=category,
                    question=question_text,
                    options_json=json.dumps(options),
                    correct_answer=options[correct_option],
                    marks=marks,
                    enabled=True,
                ))
                db.commit()
                return redirect(url_for(
                    "manage_assessment_questions", job_id=job.id,
                    round_type=round_type,
                ))
        questions = db.query(AssessmentQuestion).filter_by(
            job_id=job_id, round_type=round_type
        ).order_by(AssessmentQuestion.created_at.desc()).all()
        for question in questions:
            question.options_list = json.loads(question.options_json)
        return render_template(
            "assessment_questions.html", job=job, round_type=round_type,
            questions=questions, error=error,
        )
    finally:
        db.close()


@app.route("/admin/questions/<int:question_id>/toggle", methods=["POST"])
@recruiter_required
def toggle_assessment_question(question_id):
    db = get_db()
    try:
        question = db.query(AssessmentQuestion).filter_by(id=question_id).first()
        if not question:
            return jsonify({"error": "Question not found."}), 404
        question.enabled = not question.enabled
        db.commit()
        return redirect(url_for(
            "manage_assessment_questions", job_id=question.job_id,
            round_type=question.round_type,
        ))
    finally:
        db.close()


@app.route("/applications/<int:application_id>/assessment/<round_type>", methods=["GET", "POST"])
@login_required
def take_assessment(application_id, round_type):
    if current_user.role != "candidate" or round_type not in ("aptitude", "technical"):
        return redirect(url_for("jobs"))
    db = get_db()
    start_key = "assessment_start_{}_{}".format(application_id, round_type)
    try:
        candidate = db.query(Candidate).filter_by(name=current_user.username).first()
        application = db.query(JobApplication).filter_by(
            id=application_id,
            candidate_id=candidate.id if candidate else -1,
        ).first()
        if not application:
            return "Application not found.", 404
        job = db.query(Job).filter_by(id=application.job_id).first()
        rounds = parse_rounds(job.rounds_json)
        if not rounds[round_type]["enabled"]:
            return "This assessment round is not enabled.", 403
        if job.eligibility_enabled and application.eligibility_result != "Eligible":
            return "You did not meet the eligibility criteria for this application.", 403

        attempt = db.query(AssessmentAttempt).filter_by(
            application_id=application.id, round_type=round_type
        ).first()
        questions = db.query(AssessmentQuestion).filter_by(
            job_id=job.id, round_type=round_type, enabled=True
        ).order_by(AssessmentQuestion.id.asc()).all()
        if not questions:
            return "This assessment is not available yet.", 404
        for question in questions:
            question.options_list = json.loads(question.options_json)

        time_limit = (
            job.aptitude_time_limit if round_type == "aptitude"
            else job.technical_time_limit
        )
        if not attempt and not request.method == "POST" and not session.get(start_key):
            session[start_key] = int(time.time())
        started_at = session.get(start_key)
        remaining_seconds = max(
            0, time_limit * 60 - int(time.time() - started_at)
        ) if started_at else 0
        error = None
        result = None
        if attempt:
            result = attempt
        elif request.method == "POST":
            started_at = session.get(start_key)
            if started_at is None:
                return "Assessment session expired. Reopen the assessment to start again.", 400
            elapsed = max(0, int(time.time() - started_at))
            answers = {}
            attempted = 0
            correct = 0
            earned_marks = 0.0
            maximum_marks = sum(question.marks for question in questions)
            for question in questions:
                selected = request.form.get("answer_{}".format(question.id), "")
                valid_options = question.options_list
                if selected and selected in valid_options:
                    attempted += 1
                    answers[str(question.id)] = selected
                    if selected == question.correct_answer:
                        correct += 1
                        earned_marks += question.marks
                elif selected:
                    answers[str(question.id)] = ""
            score = round(earned_marks * 100.0 / maximum_marks, 2) if maximum_marks else 0.0
            if elapsed > time_limit * 60:
                error = "The time limit expired; your submitted answers were recorded with a score of 0."
                score = 0.0
                correct = 0
                earned_marks = 0.0
            result = AssessmentAttempt(
                application_id=application.id,
                round_type=round_type,
                total_questions=len(questions),
                attempted=attempted,
                correct=correct,
                incorrect=attempted - correct,
                marks_earned=earned_marks,
                maximum_marks=maximum_marks,
                accuracy=round(correct * 100.0 / attempted, 2) if attempted else 0.0,
                score=score,
                time_taken_seconds=min(elapsed, time_limit * 60),
                answers_json=json.dumps(answers),
            )
            db.add(result)
            if round_type == "aptitude":
                application.assessment_score = score
            else:
                application.technical_score = score
            refresh_application_score(db, application)
            db.commit()
            session.pop(start_key, None)
        return render_template(
            "assessment.html", job=job, application=application,
            round_type=round_type, questions=questions, attempt=result,
            time_limit=time_limit, remaining_seconds=remaining_seconds, error=error,
        )
    finally:
        db.close()


@app.route("/applications/<int:application_id>/interview")
@login_required
def start_application_interview(application_id):
    if current_user.role != "candidate":
        return redirect(url_for("jobs"))
    db = get_db()
    try:
        candidate = db.query(Candidate).filter_by(name=current_user.username).first()
        application = db.query(JobApplication).filter_by(
            id=application_id, candidate_id=candidate.id if candidate else -1
        ).first()
        if not application:
            return "Application not found.", 404
        job = db.query(Job).filter_by(id=application.job_id).first()
        if job.eligibility_enabled and application.eligibility_result != "Eligible":
            return "You did not meet the eligibility criteria for this application.", 403
        if not parse_rounds(job.rounds_json)["ai_interview"]["enabled"]:
            return "The AI interview round is not enabled for this job.", 403
        browser_session_id = start_interview_browser_session(
            current_user.username, job.title
        )
        interview_session = InterviewSession(
            candidate_id=candidate.id,
            session_id=browser_session_id,
            position=job.title,
            status="Active",
        )
        db.add(interview_session)
        if candidate.parsed_text:
            db.add(Resume(
                session_id=browser_session_id,
                username=candidate.name,
                position=job.title,
                filename=candidate.resume_filename or "",
                extracted_json=candidate.parsed_text,
            ))
        db.commit()
        db.refresh(interview_session)
        session["interview_session_id"] = interview_session.id
        return render_template("home.html", name=current_user.username)
    except Exception:
        db.rollback()
        app.logger.exception("Failed to start candidate interview")
        return "Unable to start the interview. Please try again.", 500
    finally:
        db.close()


@app.route("/applications/<int:application_id>/report")
@login_required
def application_report(application_id):
    db = get_db()
    try:
        application = db.query(JobApplication).filter_by(id=application_id).first()
        if not application:
            return "Application not found.", 404
        candidate = db.query(Candidate).filter_by(id=application.candidate_id).first()
        if current_user.role == "candidate" and (
            not candidate or candidate.name != current_user.username
        ):
            return "Application not found.", 404
        if current_user.role not in ("candidate", "admin", "recruiter"):
            return "Unauthorized.", 403
        job = db.query(Job).filter_by(id=application.job_id).first()
        refresh_application_score(db, application)
        ranked_applications = db.query(JobApplication).filter_by(job_id=job.id).all()
        for ranked_application in ranked_applications:
            refresh_application_score(db, ranked_application)
        ranked_applications.sort(key=lambda item: (
            item.final_score is None, -(item.final_score or 0),
            item.applied_at, item.id,
        ))
        rank = None
        last_score = None
        current_rank = 0
        for index, ranked_application in enumerate(ranked_applications):
            if ranked_application.final_score is None:
                continue
            if ranked_application.final_score != last_score:
                current_rank = index + 1
                last_score = ranked_application.final_score
            if ranked_application.id == application.id:
                rank = current_rank
                break
        db.commit()
        aptitude = db.query(AssessmentAttempt).filter_by(
            application_id=application.id, round_type="aptitude"
        ).first()
        return render_template(
            "application_report.html", application=application,
            candidate=candidate, job=job, aptitude=aptitude, rank=rank,
        )
    finally:
        db.close()


@app.route("/admin/jobs/<int:job_id>/leaderboard")
@recruiter_required
def job_leaderboard(job_id):
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return redirect(url_for("admin_jobs"))
        search = request.args.get("search", "").strip().lower()
        status_filter = request.args.get("status", "").strip()
        page = max(1, request.args.get("page", 1, type=int))
        records = db.query(JobApplication).filter_by(job_id=job_id).all()
        rows = []
        for application in records:
            candidate = db.query(Candidate).filter_by(id=application.candidate_id).first()
            if not candidate:
                continue
            refresh_application_score(db, application)
            aptitude_attempt = db.query(AssessmentAttempt).filter_by(
                application_id=application.id, round_type="aptitude"
            ).first()
            rows.append({
                "application": application,
                "candidate": candidate,
                "aptitude_score": aptitude_attempt.score if aptitude_attempt else None,
            })
        rows.sort(key=lambda row: (
            row["application"].final_score is None,
            -(row["application"].final_score or 0),
            row["application"].applied_at,
            row["candidate"].id,
        ))
        previous_score = None
        rank = 0
        for index, row in enumerate(rows):
            score = row["application"].final_score
            if score is not None:
                if score != previous_score:
                    rank = index + 1
                    previous_score = score
                row["rank"] = rank
            else:
                row["rank"] = "-"
        rows = [
            row for row in rows
            if (
                (not search
                 or search in (row["candidate"].name or "").lower()
                 or search in (row["candidate"].email or "").lower())
                and (not status_filter
                     or row["application"].status == status_filter)
            )
        ]
        db.commit()
        per_page = 20
        total_pages = (len(rows) + per_page - 1) // per_page
        page = min(page, max(total_pages, 1))
        return render_template(
            "leaderboard.html", job=job,
            applications=rows[(page - 1) * per_page:page * per_page],
            search=search, status_filter=status_filter,
            current_page=page, total_pages=total_pages,
        )
    finally:
        db.close()


@app.route("/admin/applications/<int:application_id>/status", methods=["POST"])
@recruiter_required
def update_application_status(application_id):
    db = get_db()
    try:
        application = db.query(JobApplication).filter_by(id=application_id).first()
        if not application:
            return jsonify({"error": "Application not found."}), 404
        new_status = request.form.get("status", "")
        if new_status not in APPLICATION_STATUSES:
            return jsonify({"error": "Unsupported application status."}), 400
        application.status = new_status
        db.commit()
        return redirect(url_for("job_leaderboard", job_id=application.job_id))
    finally:
        db.close()


@app.route("/admin/jobs/<int:job_id>/export.csv")
@recruiter_required
def export_job_csv(job_id):
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return "Job not found.", 404
        rows = []
        for application in db.query(JobApplication).filter_by(job_id=job_id).all():
            candidate = db.query(Candidate).filter_by(id=application.candidate_id).first()
            if candidate:
                refresh_application_score(db, application)
                aptitude = db.query(AssessmentAttempt).filter_by(
                    application_id=application.id, round_type="aptitude"
                ).first()
                rows.append((application, candidate, aptitude.score if aptitude else None))
        rows.sort(key=lambda row: (
            row[0].final_score is None, -(row[0].final_score or 0),
            row[0].applied_at, row[1].id,
        ))
        db.commit()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Rank", "Candidate", "Email", "Eligibility", "Aptitude",
            "Technical", "Coding", "AI Interview", "Final Score", "Status",
        ])
        prior_score = None
        rank = 0
        for index, (application, candidate, aptitude_score) in enumerate(rows):
            if application.final_score is not None:
                if application.final_score != prior_score:
                    rank = index + 1
                    prior_score = application.final_score
                candidate_rank = rank
            else:
                candidate_rank = ""
            writer.writerow([
                candidate_rank, candidate.name, candidate.email,
                application.eligibility_result or "",
                aptitude_score if aptitude_score is not None else "",
                application.technical_score if application.technical_score is not None else "",
                application.coding_score if application.coding_score is not None else "",
                application.interview_score if application.interview_score is not None else "",
                application.final_score if application.final_score is not None else "",
                application.status,
            ])
        response = Response(output.getvalue(), mimetype="text/csv")
        response.headers["Content-Disposition"] = (
            "attachment; filename=job_{}_leaderboard.csv".format(job_id)
        )
        return response
    finally:
        db.close()


@app.route("/admin/jobs/<int:job_id>/coding", methods=["GET", "POST"])
@recruiter_required
def manage_coding_round(job_id):
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return redirect(url_for("admin_jobs"))
        error = None
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            statement = (request.form.get("statement") or "").strip()
            tests_text = request.form.get("test_cases") or "[]"
            try:
                maximum_marks = float(request.form.get("maximum_marks") or 100)
                test_cases = json.loads(tests_text)
            except (TypeError, ValueError):
                maximum_marks, test_cases = 0, None
            if not title or not statement:
                error = "Problem title and statement are required."
            elif not isinstance(test_cases, list):
                error = "Test cases must be a JSON array."
            elif maximum_marks <= 0 or maximum_marks > 1000:
                error = "Maximum marks must be greater than 0 and no more than 1000."
            else:
                db.add(CodingProblem(
                    job_id=job.id,
                    title=title,
                    statement=statement,
                    input_description=request.form.get("input_description", ""),
                    output_description=request.form.get("output_description", ""),
                    constraints=request.form.get("constraints", ""),
                    sample_input=request.form.get("sample_input", ""),
                    sample_output=request.form.get("sample_output", ""),
                    test_cases_json=json.dumps(test_cases),
                    maximum_marks=maximum_marks,
                    enabled=True,
                ))
                db.commit()
                return redirect(url_for("manage_coding_round", job_id=job.id))
        problems = db.query(CodingProblem).filter_by(job_id=job_id).all()
        submissions = (
            db.query(CodingSubmission)
            .join(JobApplication, CodingSubmission.application_id == JobApplication.id)
            .filter(JobApplication.job_id == job_id)
            .order_by(CodingSubmission.submitted_at.desc())
            .all()
        )
        submission_rows = []
        for submission in submissions:
            application = db.query(JobApplication).filter_by(
                id=submission.application_id
            ).first()
            candidate = db.query(Candidate).filter_by(
                id=application.candidate_id
            ).first() if application else None
            problem = db.query(CodingProblem).filter_by(id=submission.problem_id).first()
            submission_rows.append({
                "submission": submission, "candidate": candidate, "problem": problem,
            })
        return render_template(
            "coding_admin.html", job=job, problems=problems,
            submissions=submission_rows, error=error,
        )
    finally:
        db.close()


@app.route("/admin/coding-submissions/<int:submission_id>/score", methods=["POST"])
@recruiter_required
def score_coding_submission(submission_id):
    db = get_db()
    try:
        submission = db.query(CodingSubmission).filter_by(id=submission_id).first()
        if not submission:
            return "Submission not found.", 404
        problem = db.query(CodingProblem).filter_by(id=submission.problem_id).first()
        if not problem:
            return "The submitted coding problem no longer exists.", 404
        try:
            score = float(request.form.get("score", ""))
        except ValueError:
            return "Score must be numeric.", 400
        if score < 0 or score > problem.maximum_marks:
            return "Score must be between 0 and the problem's maximum marks.", 400
        submission.score = score
        submission.execution_status = "Manually scored; not executed"
        application = db.query(JobApplication).filter_by(
            id=submission.application_id
        ).first()
        if not application:
            return "The application for this submission no longer exists.", 404
        submissions = db.query(CodingSubmission).filter_by(
            application_id=application.id
        ).all()
        scored = [
            (item.score, db.query(CodingProblem).filter_by(id=item.problem_id).first())
            for item in submissions if item.score is not None
        ]
        if scored:
            application.coding_score = round(
                sum(score_value * 100.0 / item_problem.maximum_marks
                    for score_value, item_problem in scored) / len(scored), 2
            )
        refresh_application_score(db, application)
        db.commit()
        return redirect(url_for(
            "manage_coding_round", job_id=problem.job_id
        ))
    finally:
        db.close()


@app.route("/applications/<int:application_id>/coding", methods=["GET", "POST"])
@login_required
def submit_coding_solution(application_id):
    if current_user.role != "candidate":
        return redirect(url_for("jobs"))
    db = get_db()
    try:
        candidate = db.query(Candidate).filter_by(name=current_user.username).first()
        application = db.query(JobApplication).filter_by(
            id=application_id, candidate_id=candidate.id if candidate else -1
        ).first()
        if not application:
            return "Application not found.", 404
        job = db.query(Job).filter_by(id=application.job_id).first()
        rounds = parse_rounds(job.rounds_json)
        if not rounds["coding"]["enabled"]:
            return "The coding round is not enabled for this job.", 403
        if job.eligibility_enabled and application.eligibility_result != "Eligible":
            return "You did not meet the eligibility criteria for this application.", 403
        problems = db.query(CodingProblem).filter_by(
            job_id=job.id, enabled=True
        ).order_by(CodingProblem.id.asc()).all()
        error = None
        if request.method == "POST":
            try:
                problem_id = int(request.form.get("problem_id", ""))
            except ValueError:
                problem_id = -1
            problem = next((item for item in problems if item.id == problem_id), None)
            language = request.form.get("language", "")
            source_code = request.form.get("source_code", "")
            if not problem or language not in ("Python", "Java"):
                error = "Choose a valid problem and Python or Java."
            elif not source_code.strip():
                error = "Source code cannot be empty."
            elif len(source_code.encode("utf-8")) > 65536:
                error = "Source code must be no larger than 64 KB."
            else:
                db.add(CodingSubmission(
                    application_id=application.id,
                    problem_id=problem.id,
                    language=language,
                    source_code=source_code,
                    execution_status="Stored for manual review; not executed",
                ))
                db.commit()
                return redirect(url_for("candidate_dashboard"))
        submissions = db.query(CodingSubmission).filter_by(
            application_id=application.id
        ).order_by(CodingSubmission.submitted_at.desc()).all()
        return render_template(
            "coding_candidate.html", job=job, problems=problems,
            submissions=submissions, error=error,
        )
    finally:
        db.close()


@app.route("/admin/jobs")
@recruiter_required
def admin_jobs():
    """Manage jobs for recruiters/admin"""
    db = get_db()
    try:
        jobs = db.query(Job).order_by(Job.created_at.desc()).all()
        for job in jobs:
            job.rounds = parse_rounds(job.rounds_json)
        return render_template('admin_jobs.html', jobs=jobs)
    finally:
        db.close()

@app.route("/admin/jobs/create", methods=["GET", "POST"])
@recruiter_required
def create_job():
    """Create a new job"""
    if request.method == "POST":
        try:
            job_values = parse_job_form(request.form)
        except ValueError as error:
            return render_template(
                'job_form.html', error=str(error), rounds=default_rounds(),
                form_values=request.form,
            )
        db = get_db()
        try:
            new_job = Job(**job_values, created_by=current_user.id)
            db.add(new_job)
            db.commit()
            return redirect(url_for('admin_jobs'))
        except Exception:
            db.rollback()
            app.logger.exception("Failed to create job")
            return render_template(
                'job_form.html', error="Failed to create job. Please try again.",
                rounds=default_rounds(), form_values=request.form,
            )
        finally:
            db.close()
    
    return render_template('job_form.html', rounds=default_rounds())

@app.route("/admin/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@recruiter_required
def edit_job(job_id):
    """Edit an existing job"""
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if not job:
            return redirect(url_for('admin_jobs'))
        
        if request.method == "POST":
            try:
                job_values = parse_job_form(request.form)
            except ValueError as error:
                return render_template(
                    'job_form.html', job=job, rounds=default_rounds(),
                    error=str(error), form_values=request.form,
                )
            for field, value in job_values.items():
                setattr(job, field, value)
            db.commit()
            return redirect(url_for('admin_jobs'))
        
        job.required_skills_list = json.loads(job.required_skills) if job.required_skills else []
        try:
            rounds = parse_rounds(job.rounds_json)
        except (TypeError, ValueError):
            app.logger.warning("Invalid saved round configuration for job %s", job.id)
            rounds = default_rounds()
        return render_template('job_form.html', job=job, rounds=rounds)
    finally:
        db.close()

@app.route("/admin/jobs/<int:job_id>/delete", methods=["POST"])
@recruiter_required
def delete_job(job_id):
    """Delete a job"""
    db = get_db()
    try:
        job = db.query(Job).filter_by(id=job_id).first()
        if job:
            db.delete(job)
            db.commit()
        return redirect(url_for('admin_jobs'))
    finally:
        db.close()

@app.route("/dashboard")
@recruiter_required
def dashboard():
    """Recruiter dashboard overview"""
    db = get_db()
    try:
        # Get total jobs posted
        total_jobs = db.query(Job).count()
        
        applications = db.query(JobApplication).all()
        for application in applications:
            refresh_application_score(db, application)
        db.commit()
        total_applicants = len(applications)
        eligible_applicants = sum(
            1 for application in applications
            if application.eligibility_result == "Eligible"
        )
        assessment_participants = db.query(AssessmentAttempt.application_id).distinct().count()
        shortlisted_applicants = sum(
            1 for application in applications
            if application.status == "Shortlisted"
        )
        selected_applicants = sum(
            1 for application in applications if application.status == "Selected"
        )
        final_scores = [
            application.final_score for application in applications
            if application.final_score is not None
        ]
        average_final_score = (
            round(sum(final_scores) / len(final_scores), 2) if final_scores else 0
        )

        # The recruiter dashboard and leaderboard use the same application scores.
        ranked_rows = []
        score_ranges = [0, 0, 0, 0, 0]
        status_counts = {status: 0 for status in APPLICATION_STATUSES}
        for application in applications:
            status_counts[application.status] = status_counts.get(application.status, 0) + 1
            if application.final_score is None:
                continue
            candidate = db.query(Candidate).filter_by(
                id=application.candidate_id
            ).first()
            job = db.query(Job).filter_by(id=application.job_id).first()
            if not candidate or not job:
                continue
            ranked_rows.append({
                "application_id": application.id,
                "job_id": job.id,
                "candidate_id": candidate.id,
                "name": candidate.name,
                "email": candidate.email,
                "position": job.title,
                "final_score": application.final_score,
                "applied_at": application.applied_at,
            })
            range_index = min(int(application.final_score // 20), 4)
            score_ranges[range_index] += 1

        ranked_rows.sort(key=lambda row: (
            row["job_id"], -row["final_score"], row["applied_at"],
            row["application_id"],
        ))
        last_job_id = None
        last_score = None
        current_rank = 0
        job_position = 0
        for row in ranked_rows:
            if row["job_id"] != last_job_id:
                last_job_id = row["job_id"]
                last_score = None
                current_rank = 0
                job_position = 0
            job_position += 1
            if row["final_score"] != last_score:
                current_rank = job_position
                last_score = row["final_score"]
            row["rank"] = current_rank

        top_candidates = sorted(
            ranked_rows,
            key=lambda row: (-row["final_score"], row["position"], row["name"]),
        )[:5]
        total_candidates = len({row["candidate_id"] for row in ranked_rows})
        
        dashboard_data = {
            'total_jobs': total_jobs,
            'total_candidates': total_candidates,
            'total_applicants': total_applicants,
            'eligible_applicants': eligible_applicants,
            'assessment_participants': assessment_participants,
            'shortlisted_applicants': shortlisted_applicants,
            'selected_applicants': selected_applicants,
            'average_final_score': average_final_score,
            'top_candidates': top_candidates,
            'score_distribution': score_ranges,
            'status_labels': list(status_counts.keys()),
            'status_counts': list(status_counts.values())
        }
        
        return render_template('dashboard.html', dashboard=dashboard_data)
    except Exception:
        app.logger.exception("Failed to load recruiter dashboard")
        return "Unable to load the recruiter dashboard. Please try again.", 500
    finally:
        db.close()

@app.route("/export/csv")
@recruiter_required
def export_csv():
    """Export all job applications with their configured round scores."""
    db = get_db()
    try:
        rows = []
        for application in db.query(JobApplication).all():
            refresh_application_score(db, application)
            candidate = db.query(Candidate).filter_by(
                id=application.candidate_id
            ).first()
            job = db.query(Job).filter_by(id=application.job_id).first()
            if not candidate or not job:
                continue
            aptitude = db.query(AssessmentAttempt).filter_by(
                application_id=application.id, round_type="aptitude"
            ).first()
            rows.append({
                "application": application,
                "candidate": candidate,
                "job": job,
                "aptitude_score": aptitude.score if aptitude else None,
            })

        rows.sort(key=lambda row: (
            row["job"].id,
            row["application"].final_score is None,
            -(row["application"].final_score or 0),
            row["application"].applied_at,
            row["application"].id,
        ))
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Job", "Rank", "Candidate", "Email", "Eligibility", "Resume",
            "Aptitude", "Technical", "Coding", "AI Interview",
            "Final Score", "Status", "Applied At",
        ])
        last_job_id = None
        last_score = None
        current_rank = 0
        job_position = 0
        for row in rows:
            application = row["application"]
            job = row["job"]
            if job.id != last_job_id:
                last_job_id = job.id
                last_score = None
                current_rank = 0
                job_position = 0
            rank = ""
            if application.final_score is not None:
                job_position += 1
                if application.final_score != last_score:
                    current_rank = job_position
                    last_score = application.final_score
                rank = current_rank
            writer.writerow([
                job.title,
                rank,
                row["candidate"].name,
                row["candidate"].email,
                application.eligibility_result or "",
                application.resume_score if application.resume_score is not None else "",
                row["aptitude_score"] if row["aptitude_score"] is not None else "",
                application.technical_score if application.technical_score is not None else "",
                application.coding_score if application.coding_score is not None else "",
                application.interview_score if application.interview_score is not None else "",
                application.final_score if application.final_score is not None else "",
                application.status,
                application.applied_at.strftime("%Y-%m-%d %H:%M"),
            ])
        db.commit()
        response = Response(output.getvalue(), mimetype="text/csv")
        response.headers["Content-Disposition"] = (
            "attachment; filename=applications_export.csv"
        )
        return response
    except Exception:
        app.logger.exception("Failed to export application CSV")
        return jsonify({"error": "Unable to export applications."}), 500
    finally:
        db.close()

@app.route("/admin")
@recruiter_required
def admin():
    """Recruiter admin dashboard with search, filters, and pagination"""
    db = get_db()
    try:
        # Get query parameters
        search_query = request.args.get('search', '').strip()
        position_filter = request.args.get('position', '').strip()
        status_filter = request.args.get('status', '').strip()
        page = request.args.get('page', 1, type=int)
        per_page = 10
        
        # Get all candidates with their interview sessions and scores
        candidates = db.query(Candidate).all()
        candidates_data = []
        
        # Get unique positions for filter dropdown
        all_positions = set()
        
        for candidate in candidates:
            # Get latest interview session
            latest_session = db.query(InterviewSession).filter_by(candidate_id=candidate.id).order_by(InterviewSession.created_at.desc()).first()
            
            if latest_session:
                all_positions.add(latest_session.position)
                
                # Calculate average scores
                scores = db.query(AIScore).filter_by(session_id=latest_session.id).all()
                avg_tech = sum(s.technical_accuracy for s in scores) / len(scores) if scores else 0
                avg_comm = sum(s.communication for s in scores) / len(scores) if scores else 0
                avg_problem = sum(s.problem_solving for s in scores) / len(scores) if scores else 0
                avg_weighted = sum(s.weighted_score for s in scores) / len(scores) if scores else 0
                
                candidates_data.append({
                    'id': candidate.id,
                    'name': candidate.name,
                    'email': candidate.email,
                    'resume_filename': candidate.resume_filename,
                    'position': latest_session.position,
                    'status': latest_session.status,
                    'avg_weighted': round(avg_weighted, 2),
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
                    'avg_weighted': 0,
                    'avg_technical': 0,
                    'avg_communication': 0,
                    'avg_problem_solving': 0,
                    'session_id': None,
                    'created_at': candidate.created_at.strftime('%Y-%m-%d %H:%M')
                })
        
        # Apply filters
        if search_query:
            search_lower = search_query.lower()
            candidates_data = [c for c in candidates_data if search_lower in c['name'].lower() or search_lower in c['email'].lower()]
        
        if position_filter:
            candidates_data = [c for c in candidates_data if c['position'] == position_filter]
        
        if status_filter:
            candidates_data = [c for c in candidates_data if c['status'] == status_filter]
        
        # Sort candidates by weighted score (descending) and add rank
        candidates_with_scores = [c for c in candidates_data if c['avg_weighted'] > 0]
        candidates_with_scores.sort(key=lambda x: x['avg_weighted'], reverse=True)
        
        for idx, candidate in enumerate(candidates_with_scores):
            if idx == 0:
                candidate['rank'] = 1
            else:
                if candidate['avg_weighted'] == candidates_with_scores[idx - 1]['avg_weighted']:
                    candidate['rank'] = candidates_with_scores[idx - 1]['rank']
                else:
                    candidate['rank'] = idx + 1
        
        ranked_dict = {c['id']: c['rank'] for c in candidates_with_scores}
        for candidate in candidates_data:
            candidate['rank'] = ranked_dict.get(candidate['id'], '-')
        
        candidates_data.sort(key=lambda x: (x['rank'] == '-', x['rank'] if x['rank'] != '-' else 999, x['name']))
        
        # Pagination
        total_candidates = len(candidates_data)
        total_pages = (total_candidates + per_page - 1) // per_page
        page = max(1, min(page, total_pages)) if total_pages > 0 else 1
        
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_candidates = candidates_data[start_idx:end_idx]
        
        return render_template('admin.html', 
                              candidates=paginated_candidates,
                              positions=sorted(list(all_positions)),
                              search_query=search_query,
                              position_filter=position_filter,
                              status_filter=status_filter,
                              current_page=page,
                              total_pages=total_pages)
    except Exception as e:
        print(f"Error fetching admin data: {e}")
        return render_template('admin.html', 
                              candidates=[],
                              positions=[],
                              search_query='',
                              position_filter='',
                              status_filter='',
                              current_page=1,
                              total_pages=1)
    finally:
        db.close()

@app.route("/admin/candidate/<int:session_id>/update_status", methods=["POST"])
@recruiter_required
def update_candidate_status(session_id):
    """Update candidate interview status and send notification"""
    db = get_db()
    try:
        session = db.query(InterviewSession).filter_by(id=session_id).first()
        if not session:
            return jsonify({'error': 'Session not found'}), 404
        
        new_status = request.json.get('status')
        if new_status and new_status != session.status:
            old_status = session.status
            session.status = new_status
            db.commit()
            
            # Send email notification if status changed to Completed
            if new_status == 'Completed':
                candidate = db.query(Candidate).filter_by(id=session.candidate_id).first()
                if candidate and candidate.email:
                    scores = db.query(AIScore).filter_by(session_id=session_id).all()
                    avg_score = sum(s.weighted_score for s in scores) / len(scores) if scores else 0
                    
                    subject = "Interview Completed - AI Interview Platform"
                    body = f"""
                    <html>
                    <body>
                        <h2>Interview Completed</h2>
                        <p>Dear {candidate.name},</p>
                        <p>Your interview for the position of <strong>{session.position}</strong> has been completed.</p>
                        <p><strong>Your weighted score: {round(avg_score, 2)}/10</strong></p>
                        <p>Thank you for participating in the interview process.</p>
                        <p>Best regards,<br>AI Interview Platform Team</p>
                    </body>
                    </html>
                    """
                    send_email_notification(candidate.email, subject, body)
        
        return jsonify({'success': True, 'status': session.status})
    except Exception as e:
        print(f"Error updating status: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()

@app.route("/admin/candidate/<int:session_id>")
@recruiter_required
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
@candidate_required
def home():
    if request.method == "POST":
        username = current_user.username
        pos = (request.form.get("position") or "").strip()
        if not pos:
            return render_template(
                "index.html", error="Choose an interview position."
            ), 400
        session_id = start_interview_browser_session(username, pos)
        display_name = username
        
        # Create candidate and interview session in database
        db = get_db()
        try:
            # Check if candidate already exists
            existing_candidate = get_or_create_candidate(db, username)
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
                    return render_template('home.html', name=display_name, error="Invalid file type. Only PDF, DOCX, and TXT files are allowed.")
                
                # Validate MIME type (magic bytes check)
                if not validate_mime_type(resume_file, resume_file.filename):
                    return render_template('home.html', name=display_name, error="File content does not match the file extension. Please upload a valid file.")
                
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
                        return render_template('home.html', name=display_name, error="Resume must contain at least an email or phone number.")
                    
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
                        return render_template('home.html', name=display_name, error="Failed to save resume data to database.")
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
                    return render_template('home.html', name=display_name, error=f"Resume validation failed: {str(e)}")
                except ResumeParsingError as e:
                    print(f"Resume parsing error: {e}")
                    # Clean up uploaded file
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return render_template('home.html', name=display_name, error=f"Resume parsing failed: {str(e)}. Please ensure the file is not corrupted.")
                except Exception as e:
                    print(f"Unexpected error during resume processing: {e}")
                    # Clean up uploaded file
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return render_template('home.html', name=display_name, error=f"An error occurred while processing your resume. Please try again.")
        
        return render_template('home.html', name=display_name, resume_data=parsed_data)
    return render_template('index.html', name=current_user.username)

@app.route("/get_flag", methods=["GET"])
@candidate_interview_required
def get_flag():
    return jsonify({'flag': session.get('grammar_feedback', '')})

@app.route("/get", methods=["GET", "POST"])
@candidate_interview_required
def chat_():
    msg = request.form["msg"]
    input = msg
    return get_Chat_response(input)

@app.route("/get_stream", methods=["POST"])
@candidate_interview_required
def chat_stream():
    """Streaming endpoint for real-time response"""
    msg = request.form["msg"]
    return Response(stream_with_context(get_Chat_response_stream(msg)), mimetype='text/plain')

@app.route('/upload', methods=['POST'])
@candidate_interview_required
def upload_audio():
    if 'audio' in request.files:
        audio_file = request.files['audio']
        audio_file.save(interview_audio_path())
        print("Audio saved successfully")
        return 'Audio uploaded successfully', 200
    else:
        print("Audio not saved")
        return 'No audio file received', 400

DEFAULT_CHUNK_LENGTH = 10

@app.route('/get_text', methods=['GET'])
@candidate_interview_required
def get_text():
    audio_path = interview_audio_path()
    if not os.path.exists(audio_path):
        return 'No audio file received', 400
    result = get_audio_model().transcribe(audio_path)
    segments, info = result
    print("Detected language '%s' with probability %f" % (info.language, info.language_probability))
    text = ""
    for segment in segments:
        text += segment.text
    session['speaking_pace'] = calculate_speaking_pace(text, chunk_length=DEFAULT_CHUNK_LENGTH)
    session.modified = True
    return text

@app.route('/get_pace', methods=['GET'])
@candidate_interview_required
def get_pace():
    flag = session.get('interview_flag', 0)
    pace = session.get('speaking_pace', 0.0)
    output = pace_checker(pace) if flag else ""
    return jsonify({'Pace_Checker': output})


def get_Chat_response(text):
    role = session.get('interview_position') or "Software Developer"
    flag = session.get('interview_flag', 0)
    prev_q = list(session.get('prev_q') or [])

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
    session['grammar_feedback'] = feed

    output = output.strip()

    if len(output) > 200:
        output = output[:200]

    prev_q.append(output)
    session['interview_flag'] = flag
    session['prev_q'] = prev_q
    session.modified = True

    return output

def get_Chat_response_stream(text):
    """Streaming version of chat response with AI scoring"""
    # Check for quit intent
    quit_keywords = ['quit', 'exit', 'stop', 'end interview', 'finish']
    if any(keyword in text.lower() for keyword in quit_keywords):
        interview_session_id = session.get("interview_session_id")
        if interview_session_id:
            db = get_db()
            try:
                interview_session = db.query(InterviewSession).filter_by(
                    id=interview_session_id
                ).first()
                if interview_session:
                    interview_session.status = "Completed"
                    interview_session.completed_at = datetime.utcnow()
                    refresh_interview_application_scores(db, interview_session_id)
                    db.commit()
            except Exception:
                db.rollback()
                app.logger.exception("Failed to complete interview session")
            finally:
                db.close()
        yield "INTERVIEW_END"
        return

    role = session.get('interview_position') or "Software Developer"
    flag = session.get('interview_flag', 0)
    prev_q = list(session.get('prev_q') or [])

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
    session['interview_flag'] = flag
    session['prev_q'] = prev_q
    session.modified = True

    # Generate AI score for the answer
    score_prompt = f"""
You are an experienced interviewer evaluating a candidate's answer.

Position: {role}
Question: {previous_question if flag > 1 else "Tell me about yourself"}
Candidate Answer: {text}

Evaluate the answer and return ONLY a JSON object with this exact format:
{{"technical_accuracy": X, "communication": Y, "problem_solving": Z, "feedback": "..."}}

Score each dimension from 1-10:
- technical_accuracy: correctness and quality of the technical/content details.
- communication: relevance to the question and whether the answer stays on topic.
- problem_solving: completeness of the explanation and reasoning.
Feedback must briefly explain the scores. Do not infer personality, emotion, or honesty.
"""

    try:
        score_response = generate_score(score_prompt)
        # Parse JSON from response
        import re
        json_match = re.search(r'\{.*\}', score_response, re.DOTALL)
        if json_match:
            score_data = json.loads(json_match.group())
            score_fields = (
                "technical_accuracy", "communication", "problem_solving"
            )
            valid_ratings = all(
                type(score_data.get(field)) is int
                and 1 <= score_data[field] <= 10
                for field in score_fields
            )
            if not valid_ratings or not isinstance(score_data.get("feedback"), str):
                app.logger.warning("Gemini returned an invalid interview score.")
            elif 'interview_session_id' in session:
                db = get_db()
                try:
                    ai_score = AIScore(
                        session_id=session['interview_session_id'],
                        question=previous_question if flag > 1 else "Tell me about yourself",
                        answer=text,
                        technical_accuracy=score_data["technical_accuracy"],
                        communication=score_data["communication"],
                        problem_solving=score_data["problem_solving"],
                        feedback=score_data["feedback"]
                    )
                    db.add(ai_score)
                    db.flush()
                    refresh_interview_application_scores(
                        db, session['interview_session_id']
                    )
                    db.commit()
                except Exception as e:
                    db.rollback()
                    app.logger.exception("Failed to save interview score")
                finally:
                    db.close()
    except Exception as e:
        app.logger.exception("Failed to generate interview score")

    # Grammar check (legacy)
    feed = grammar_checker(text)
    session['grammar_feedback'] = feed
    session.modified = True

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
   You are interviewing a candidate for the position of {session.get('interview_position') or 'Software Developer'} .\
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