from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response, stream_with_context
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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from resume_parser import parse_resume, FileValidationError, ResumeParsingError
from database import init_db, get_db, Resume, Candidate, InterviewSession, AIScore, User, Job, JobApplication
import google.generativeai as genai

model_audio = WhisperModel(model_size_or_path="small")

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
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

def sanitize_input(input_string):
    """Basic input sanitization to prevent XSS"""
    if not input_string:
        return ""
    # Remove script tags and basic HTML
    sanitized = re.sub(r'<script.*?>.*?</script>', '', input_string, flags=re.IGNORECASE | re.DOTALL)
    sanitized = re.sub(r'<[^>]+>', '', sanitized)
    return sanitized.strip()

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
        
        candidate = get_or_create_candidate(
            db, current_user.username, current_user.email or ''
        )
        
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
@recruiter_required
def admin_jobs():
    """Manage jobs for recruiters/admin"""
    db = get_db()
    try:
        jobs = db.query(Job).order_by(Job.created_at.desc()).all()
        return render_template('admin_jobs.html', jobs=jobs)
    finally:
        db.close()

@app.route("/admin/jobs/create", methods=["GET", "POST"])
@recruiter_required
def create_job():
    """Create a new job"""
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
@recruiter_required
def edit_job(job_id):
    """Edit an existing job"""
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
        
        # Get total candidates evaluated (with interview sessions)
        total_candidates = db.query(InterviewSession).distinct(InterviewSession.candidate_id).count()
        
        # Get top-ranked candidates (with weighted scores)
        candidates = db.query(Candidate).all()
        candidates_with_scores = []
        
        # Calculate score distribution for charts
        score_ranges = [0, 0, 0, 0, 0]  # 0-2, 2-4, 4-6, 6-8, 8-10
        
        for candidate in candidates:
            latest_session = db.query(InterviewSession).filter_by(candidate_id=candidate.id).order_by(InterviewSession.created_at.desc()).first()
            if latest_session:
                scores = db.query(AIScore).filter_by(session_id=latest_session.id).all()
                avg_weighted = sum(s.weighted_score for s in scores) / len(scores) if scores else 0
                if avg_weighted > 0:
                    candidates_with_scores.append({
                        'name': candidate.name,
                        'email': candidate.email,
                        'position': latest_session.position,
                        'avg_weighted': round(avg_weighted, 2),
                        'session_id': latest_session.id
                    })
                    
                    # Add to score distribution
                    if avg_weighted <= 2:
                        score_ranges[0] += 1
                    elif avg_weighted <= 4:
                        score_ranges[1] += 1
                    elif avg_weighted <= 6:
                        score_ranges[2] += 1
                    elif avg_weighted <= 8:
                        score_ranges[3] += 1
                    else:
                        score_ranges[4] += 1
        
        # Sort by weighted score and get top 5
        candidates_with_scores.sort(key=lambda x: x['avg_weighted'], reverse=True)
        top_candidates = candidates_with_scores[:5]
        
        # Assign ranks
        for idx, candidate in enumerate(top_candidates):
            candidate['rank'] = idx + 1
        
        # Get status distribution
        sessions = db.query(InterviewSession).all()
        status_counts = {'Active': 0, 'Completed': 0, 'Inactive': 0}
        for session in sessions:
            if session.status in status_counts:
                status_counts[session.status] += 1
        
        dashboard_data = {
            'total_jobs': total_jobs,
            'total_candidates': total_candidates,
            'top_candidates': top_candidates,
            'score_distribution': score_ranges,
            'status_labels': list(status_counts.keys()),
            'status_counts': list(status_counts.values())
        }
        
        return render_template('dashboard.html', dashboard=dashboard_data)
    except Exception as e:
        print(f"Error fetching dashboard data: {e}")
        return render_template('dashboard.html', dashboard={'total_jobs': 0, 'total_candidates': 0, 'top_candidates': [], 'score_distribution': [0, 0, 0, 0, 0], 'status_labels': ['Active', 'Completed', 'Inactive'], 'status_counts': [0, 0, 0]})
    finally:
        db.close()

@app.route("/export/csv")
@recruiter_required
def export_csv():
    """Export ranked candidates data as CSV"""
    db = get_db()
    try:
        candidates = db.query(Candidate).all()
        candidates_data = []
        
        for candidate in candidates:
            latest_session = db.query(InterviewSession).filter_by(candidate_id=candidate.id).order_by(InterviewSession.created_at.desc()).first()
            if latest_session:
                scores = db.query(AIScore).filter_by(session_id=latest_session.id).all()
                avg_weighted = sum(s.weighted_score for s in scores) / len(scores) if scores else 0
                if avg_weighted > 0:
                    candidates_data.append({
                        'name': candidate.name,
                        'email': candidate.email,
                        'position': latest_session.position,
                        'status': latest_session.status,
                        'avg_weighted': round(avg_weighted, 2),
                        'created_at': latest_session.created_at.strftime('%Y-%m-%d %H:%M')
                    })
        
        candidates_data.sort(key=lambda x: x['avg_weighted'], reverse=True)
        
        for idx, candidate in enumerate(candidates_data):
            candidate['rank'] = idx + 1
        
        def generate():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['Rank', 'Name', 'Email', 'Position', 'Status', 'Weighted Score', 'Date'])
            for c in candidates_data:
                writer.writerow([c['rank'], c['name'], c['email'], c['position'], c['status'], c['avg_weighted'], c['created_at']])
            output.seek(0)
            yield output.getvalue()
        
        response = Response(generate(), mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=candidates_export.csv'
        return response
    except Exception as e:
        print(f"Error exporting CSV: {e}")
        return jsonify({'error': str(e)}), 500
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
def home():
    if request.method == "POST":
        username = request.form["username"]
        pos = request.form["position"]
        print(username)
        print(pos)
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
    return render_template('home.html', name=session.get('candidate_name'))

@app.route("/get_flag", methods=["GET"])
def get_flag():
    return jsonify({'flag': session.get('grammar_feedback', '')})

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
        audio_file.save(interview_audio_path())
        print("Audio saved successfully")
        return 'Audio uploaded successfully', 200
    else:
        print("Audio not saved")
        return 'No audio file received', 400

DEFAULT_CHUNK_LENGTH = 10

@app.route('/get_text', methods=['GET'])
def get_text():
    audio_path = interview_audio_path()
    if not os.path.exists(audio_path):
        return 'No audio file received', 400
    result = model_audio.transcribe(audio_path)
    segments, info = result
    print("Detected language '%s' with probability %f" % (info.language, info.language_probability))
    text = ""
    for segment in segments:
        text += segment.text
    session['speaking_pace'] = calculate_speaking_pace(text, chunk_length=DEFAULT_CHUNK_LENGTH)
    session.modified = True
    return text

@app.route('/get_pace', methods=['GET'])
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