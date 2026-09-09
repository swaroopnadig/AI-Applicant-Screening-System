"""
Database Configuration and Models
Uses SQLAlchemy with SQLite for local development.
Can be easily swapped to MySQL/PostgreSQL by changing DATABASE_URL.
"""

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from flask_login import UserMixin
from datetime import datetime
import os

# Database URL - Change this to switch databases
# SQLite (default for local development):
DATABASE_URL = "sqlite:///interview.db"
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


class User(Base, UserMixin):
    """User model for authentication"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="candidate")  # candidate, recruiter, admin
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False


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
    weight_skills = Column(Integer, default=40)  # weight percentage
    weight_experience = Column(Integer, default=30)  # weight percentage
    weight_education = Column(Integer, default=30)  # weight percentage
    weight_resume = Column(Float, default=30.0)
    weight_ai = Column(Float, default=30.0)
    cutoff_score = Column(Float, default=75.0)
    scenario_prompt = Column(Text, nullable=True)
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
    evaluations = relationship("CandidateEvaluation", back_populates="application")


class CandidateEvaluation(Base):
    """Auditable composite screening score for a job application."""
    __tablename__ = "candidate_evaluations"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), unique=True, nullable=False)
    resume_score = Column(Float, default=0.0, nullable=False)
    skills_score = Column(Float, default=0.0, nullable=False)
    ai_response_score = Column(Float, default=0.0, nullable=False)
    final_weighted_score = Column(Float, default=0.0, nullable=False)
    status = Column(String(20), default="Pending", nullable=False)
    breakdown_json = Column(Text, default="{}")
    evaluated_at = Column(DateTime, nullable=True)

    application = relationship("JobApplication", back_populates="evaluations")


def init_db():
    """Initialize database tables"""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns():
    """Add newly introduced Job columns to existing development databases."""
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()
            }
    else:
        from sqlalchemy import inspect
        columns = {column["name"] for column in inspect(engine).get_columns("jobs")}

    additions = {
        "weight_resume": "FLOAT DEFAULT 30.0",
        "weight_ai": "FLOAT DEFAULT 30.0",
        "cutoff_score": "FLOAT DEFAULT 75.0",
        "scenario_prompt": "TEXT",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        return db
    except Exception:
        db.close()
        raise
