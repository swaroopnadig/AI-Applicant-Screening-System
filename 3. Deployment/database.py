"""
Database Configuration and Models
Uses SQLAlchemy with SQLite for local development.
Can be easily swapped to MySQL/PostgreSQL by changing DATABASE_URL.
"""

from flask_login import UserMixin
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os

# Database URL - Change this to switch databases
# SQLite (default for local development):
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///interview.db")
# MySQL: "mysql+pymysql://user:password@localhost/interview_db"
# PostgreSQL: "postgresql://user:password@localhost/interview_db"

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Resume(Base):
    """Resume model for storing parsed resume data"""
    __tablename__ = "resumes"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), index=True)  # Session identifier
    username = Column(String(100))  # User name
    position = Column(String(100))  # Job position applied for
    filename = Column(String(255))  # Original filename
    parsed_text = Column(Text)  # Full extracted text
    extracted_json = Column(Text)  # JSON string of extracted data
    created_at = Column(DateTime, default=datetime.utcnow)


class Candidate(Base):
    """Candidate model for storing candidate information"""
    __tablename__ = "candidates"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100))  # Candidate name
    email = Column(String(255))  # Candidate email
    resume_filename = Column(String(255))  # Resume filename
    parsed_text = Column(Text)  # Parsed resume text
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship with interview sessions
    interview_sessions = relationship("InterviewSession", back_populates="candidate")


class InterviewSession(Base):
    """Interview session model for tracking interview progress"""
    __tablename__ = "interview_sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"))  # Foreign key to candidate
    session_id = Column(String(100), unique=True, index=True)  # Unique session identifier
    position = Column(String(100))  # Job position
    status = Column(String(20), default="Active")  # Active/Completed
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)  # When interview was completed
    
    # Relationships
    candidate = relationship("Candidate", back_populates="interview_sessions")
    ai_scores = relationship("AIScore", back_populates="session")


class AIScore(Base):
    """AI score model for storing evaluation metrics per answer"""
    __tablename__ = "ai_scores"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id"))  # Foreign key to session
    question = Column(Text)  # Interview question asked
    answer = Column(Text)  # Candidate's answer
    technical_accuracy = Column(Integer)  # Technical score (1-10)
    communication = Column(Integer)  # Communication score (1-10)
    problem_solving = Column(Integer)  # Problem solving score (1-10)
    feedback = Column(Text)  # Detailed feedback text
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship
    session = relationship("InterviewSession", back_populates="ai_scores")

    @property
    def weighted_score(self):
        """Return the legacy interview score on its existing 0-10 scale."""
        values = (
            self.technical_accuracy or 0,
            self.communication or 0,
            self.problem_solving or 0,
        )
        return sum(values) / len(values)


class User(UserMixin, Base):
    """User model for authentication"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="candidate")  # candidate, recruiter, admin
    created_at = Column(DateTime, default=datetime.utcnow)


class Job(Base):
    """Job model for job postings"""
    __tablename__ = "jobs"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    company = Column(String(200), nullable=False)
    location = Column(String(200))
    description = Column(Text)
    required_skills = Column(Text)  # JSON array of skills
    minimum_experience = Column(Integer, default=0)  # years
    minimum_education = Column(String(100))
    minimum_cgpa = Column(Float, nullable=True)
    eligibility_enabled = Column(Boolean, nullable=False, default=False)
    rounds_json = Column(Text, nullable=False, default='{"resume_screening":{"enabled":true,"weight":20},"aptitude":{"enabled":true,"weight":20},"technical":{"enabled":true,"weight":20},"coding":{"enabled":false,"weight":0},"ai_interview":{"enabled":true,"weight":40}}')
    aptitude_time_limit = Column(Integer, nullable=False, default=30)
    technical_time_limit = Column(Integer, nullable=False, default=30)
    weight_skills = Column(Integer, default=40)  # weight percentage
    weight_experience = Column(Integer, default=30)  # weight percentage
    weight_education = Column(Integer, default=30)  # weight percentage
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"))


class JobApplication(Base):
    """Job application model to track candidate applications"""
    __tablename__ = "job_applications"
    
    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"))
    candidate_id = Column(Integer, ForeignKey("candidates.id"))
    applied_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="Applied")  # Applied, Reviewing, Interviewed, Rejected, Hired
    eligibility_result = Column(String(20), nullable=True)
    eligibility_details = Column(Text, nullable=True)
    resume_score = Column(Float, nullable=True)
    assessment_score = Column(Float, nullable=True)
    technical_score = Column(Float, nullable=True)
    coding_score = Column(Float, nullable=True)
    interview_score = Column(Float, nullable=True)
    final_score = Column(Float, nullable=True)


class AssessmentQuestion(Base):
    """Reusable multiple-choice question assigned to a job recruitment round."""
    __tablename__ = "assessment_questions"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    round_type = Column(String(20), nullable=False)  # aptitude or technical
    category = Column(String(100), nullable=False)
    question = Column(Text, nullable=False)
    options_json = Column(Text, nullable=False)
    correct_answer = Column(String(500), nullable=False)
    marks = Column(Float, nullable=False, default=1)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AssessmentAttempt(Base):
    """A candidate's submitted and automatically scored assessment."""
    __tablename__ = "assessment_attempts"
    __table_args__ = (UniqueConstraint("application_id", "round_type", name="uq_assessment_application_round"),)

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False, index=True)
    round_type = Column(String(20), nullable=False)
    total_questions = Column(Integer, nullable=False)
    attempted = Column(Integer, nullable=False)
    correct = Column(Integer, nullable=False)
    incorrect = Column(Integer, nullable=False)
    marks_earned = Column(Float, nullable=False)
    maximum_marks = Column(Float, nullable=False)
    accuracy = Column(Float, nullable=False)
    score = Column(Float, nullable=False)
    answers_json = Column(Text, nullable=False)
    time_taken_seconds = Column(Integer, nullable=False, default=0)
    submitted_at = Column(DateTime, default=datetime.utcnow)


class CodingProblem(Base):
    """Coding-round prompt and visible samples; submissions are not executed on-host."""
    __tablename__ = "coding_problems"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    statement = Column(Text, nullable=False)
    input_description = Column(Text, nullable=True)
    output_description = Column(Text, nullable=True)
    constraints = Column(Text, nullable=True)
    sample_input = Column(Text, nullable=True)
    sample_output = Column(Text, nullable=True)
    test_cases_json = Column(Text, nullable=False, default="[]")
    maximum_marks = Column(Float, nullable=False, default=100)
    enabled = Column(Boolean, nullable=False, default=True)


class CodingSubmission(Base):
    """Stored candidate code for later evaluation in an isolated sandbox."""
    __tablename__ = "coding_submissions"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False, index=True)
    problem_id = Column(Integer, ForeignKey("coding_problems.id"), nullable=False, index=True)
    language = Column(String(20), nullable=False)
    source_code = Column(Text, nullable=False)
    execution_status = Column(String(100), nullable=False, default="Pending sandbox evaluation")
    score = Column(Float, nullable=True)
    submitted_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    """Initialize database tables"""
    Base.metadata.create_all(bind=engine)
    _upgrade_existing_sqlite_schema()


def _upgrade_existing_sqlite_schema():
    """Add new nullable/defaulted columns without replacing existing SQLite data."""
    if engine.dialect.name != "sqlite":
        return

    additions = {
        "jobs": {
            "minimum_cgpa": "FLOAT",
            "eligibility_enabled": "BOOLEAN NOT NULL DEFAULT 0",
            "aptitude_time_limit": "INTEGER NOT NULL DEFAULT 30",
            "technical_time_limit": "INTEGER NOT NULL DEFAULT 30",
            "rounds_json": """TEXT NOT NULL DEFAULT
                '{"resume_screening":{"enabled":true,"weight":20},"aptitude":{"enabled":true,"weight":20},"technical":{"enabled":true,"weight":20},"coding":{"enabled":false,"weight":0},"ai_interview":{"enabled":true,"weight":40}}'""",
        },
        "job_applications": {
            "eligibility_result": "VARCHAR(20)",
            "eligibility_details": "TEXT",
            "resume_score": "FLOAT",
            "assessment_score": "FLOAT",
            "technical_score": "FLOAT",
            "coding_score": "FLOAT",
            "interview_score": "FLOAT",
            "final_score": "FLOAT",
        },
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {
                row[1] for row in connection.exec_driver_sql(
                    "PRAGMA table_info({})".format(table)
                ).fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        "ALTER TABLE {} ADD COLUMN {} {}".format(table, name, definition)
                    )


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise
