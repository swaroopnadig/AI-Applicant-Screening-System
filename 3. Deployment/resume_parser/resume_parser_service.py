"""
Resume Parser Service Module

Provides a validated, error-handled interface for resume parsing.
Supports PDF, DOCX, and TXT file formats with comprehensive validation.
"""

import os
import io
import logging
from typing import Dict, Optional, Union
from pathlib import Path


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FileValidationError(Exception):
    """Raised when file validation fails"""
    pass


class ResumeParsingError(Exception):
    """Raised when resume parsing fails"""
    pass


class ResumeParserService:
    """
    Service layer for resume parsing with validation and error handling.
    
    Provides a clean, reusable interface for parsing resumes from various
    file formats with comprehensive validation and error handling.
    """
    
    # Supported file extensions
    SUPPORTED_EXTENSIONS = {'.pdf', '.docx', '.txt'}
    
    # Maximum file size in bytes (10MB)
    MAX_FILE_SIZE = 10 * 1024 * 1024
    
    def __init__(self, skills_file: Optional[str] = None, custom_regex: Optional[str] = None):
        """
        Initialize the ResumeParserService.
        
        Args:
            skills_file: Optional path to custom skills CSV file
            custom_regex: Optional custom regex for mobile number extraction
        """
        self.skills_file = skills_file
        self.custom_regex = custom_regex
    
    def parse_resume(
        self,
        file_path: Union[str, Path, io.BytesIO]
    ) -> Dict:
        """
        Parse a resume file and return structured data.
        
        Args:
            file_path: Path to resume file (str, Path, or BytesIO object)
            
        Returns:
            Dictionary containing parsed resume data
            
        Raises:
            FileValidationError: If file validation fails
            ResumeParsingError: If parsing fails
        """
        try:
            # Validate and prepare file
            validated_file = self._validate_and_prepare_file(file_path)
            
            # Parse the resume using basic parsing (reliable, no custom model dependency)
            logger.info(f"Parsing resume: {file_path if isinstance(file_path, (str, Path)) else 'BytesIO object'}")
            data = self._parse_resume_basic(validated_file)
            
            logger.info("Resume parsed successfully")
            
            return data
            
        except FileValidationError:
            raise
        except Exception as e:
            logger.error(f"Failed to parse resume: {str(e)}")
            raise ResumeParsingError(f"Resume parsing failed: {str(e)}")
    
    def _parse_resume_basic(self, file_path: Union[str, io.BytesIO]) -> Dict:
        """
        Basic resume parsing without custom NER model as fallback.
        Uses regex-based extraction for core functionality without spacy dependency.
        
        Args:
            file_path: Path to file or BytesIO object
            
        Returns:
            Dictionary containing basic parsed resume data
        """
        from . import utils
        
        # Get file extension
        if isinstance(file_path, io.BytesIO):
            ext = file_path.name.split('.')[1]
        else:
            ext = os.path.splitext(file_path)[1].split('.')[1]
        
        # Extract text
        logger.info(f"Extracting text from {ext} file")
        text_raw = utils.extract_text(file_path, '.' + ext)
        text = ' '.join(text_raw.split())
        
        # Extract basic details
        details = {
            'name': None,
            'email': None,
            'mobile_number': None,
            'skills': None,
            'college_name': None,
            'degree': None,
            'designation': None,
            'experience': None,
            'company_names': None,
            'no_of_pages': None,
            'total_experience': None,
        }
        
        # Extract email
        try:
            details['email'] = utils.extract_email(text)
            logger.info("Email extracted successfully")
        except Exception as e:
            logger.warning(f"Email extraction failed: {str(e)}")
        
        # Extract mobile
        try:
            details['mobile_number'] = utils.extract_mobile_number(text, self.custom_regex)
            logger.info("Mobile extracted successfully")
        except Exception as e:
            logger.warning(f"Mobile extraction failed: {str(e)}")
        
        # Extract skills using basic keyword matching (no spacy)
        try:
            details['skills'] = self._extract_skills_basic(text)
            logger.info("Skills extracted successfully")
        except Exception as e:
            logger.warning(f"Skills extraction failed: {str(e)}")
        
        # Extract name using basic heuristics
        try:
            details['name'] = self._extract_name_basic(text)
            logger.info("Name extracted successfully")
        except Exception as name_error:
            logger.warning(f"Name extraction failed: {str(name_error)}")
        
        # Extract entities from sections
        try:
            entities = utils.extract_entity_sections_grad(text_raw)
            logger.info("Entity sections extracted successfully")
        except Exception as e:
            logger.warning(f"Entity sections extraction failed: {str(e)}")
            entities = {}
        
        # Extract college name
        try:
            details['college_name'] = entities.get('College Name')
        except Exception:
            pass
        
        # Extract experience
        try:
            details['experience'] = entities.get('experience')
            try:
                exp = round(utils.get_total_experience(entities['experience']) / 12, 2)
                details['total_experience'] = exp
            except Exception:
                details['total_experience'] = 0
        except Exception:
            details['total_experience'] = 0
        
        # Get number of pages
        try:
            details['no_of_pages'] = utils.get_number_of_pages(file_path)
            logger.info("Page count extracted successfully")
        except Exception as e:
            logger.warning(f"Page count extraction failed: {str(e)}")
        
        return details
    
    def _extract_skills_basic(self, text: str) -> list:
        """
        Extract skills using basic keyword matching without spacy.
        
        Args:
            text: Resume text
            
        Returns:
            List of extracted skills
        """
        import pandas as pd
        import re
        
        # Load skills from CSV
        try:
            if self.skills_file:
                data = pd.read_csv(self.skills_file)
            else:
                data = pd.read_csv(
                    os.path.join(os.path.dirname(__file__), 'skills.csv')
                )
            skills = list(data.columns.values)
        except Exception:
            # Fallback to common tech skills if CSV fails
            skills = [
                'python', 'java', 'javascript', 'c++', 'c#', 'sql', 'html', 'css',
                'react', 'angular', 'node.js', 'django', 'flask', 'spring', 'aws',
                'docker', 'kubernetes', 'git', 'machine learning', 'data science',
                'tensorflow', 'pytorch', 'pandas', 'numpy', 'linux', 'ubuntu'
            ]
        
        # Find skills in text (case-insensitive)
        text_lower = text.lower()
        found_skills = []
        
        for skill in skills:
            skill_lower = skill.lower()
            if skill_lower in text_lower:
                found_skills.append(skill)
        
        return [s.capitalize() for s in set(found_skills)]
    
    def _extract_name_basic(self, text: str) -> str:
        """
        Extract name using basic heuristics without spacy.
        
        Args:
            text: Resume text
            
        Returns:
            Extracted name or None
        """
        import re
        
        # Simple heuristic: first line that looks like a name
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        for line in lines:
            # Skip if it's an email or phone
            if '@' in line or re.search(r'\d{3}[-.\s]?\d{3}[-.\s]?\d{4}', line):
                continue
            
            # Skip if it's a section header
            if any(word in line.lower() for word in ['experience', 'education', 'skills', 'summary', 'objective']):
                continue
            
            # Check if line looks like a name (2-3 words, each starting with capital)
            words = line.split()
            if 2 <= len(words) <= 3:
                if all(word[0].isupper() for word in words if word):
                    return line
        
        return None
    
    def _validate_and_prepare_file(
        self,
        file_path: Union[str, Path, io.BytesIO]
    ) -> Union[str, io.BytesIO]:
        """
        Validate file and prepare for parsing.
        
        Args:
            file_path: Path to file or BytesIO object
            
        Returns:
            Validated file path or BytesIO object
            
        Raises:
            FileValidationError: If validation fails
        """
        # Handle BytesIO objects
        if isinstance(file_path, io.BytesIO):
            return self._validate_bytesio_file(file_path)
        
        # Handle string and Path objects
        file_path = str(file_path)
        return self._validate_file_path(file_path)
    
    def _validate_file_path(self, file_path: str) -> str:
        """
        Validate a file path.
        
        Args:
            file_path: Path to file
            
        Returns:
            Validated file path
            
        Raises:
            FileValidationError: If validation fails
        """
        # Check if file exists
        if not os.path.exists(file_path):
            raise FileValidationError(f"File not found: {file_path}")
        
        # Check if it's a file (not directory)
        if not os.path.isfile(file_path):
            raise FileValidationError(f"Path is not a file: {file_path}")
        
        # Check file extension
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise FileValidationError(
                f"Unsupported file extension: {ext}. "
                f"Supported extensions: {', '.join(self.SUPPORTED_EXTENSIONS)}"
            )
        
        # Check file size
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            raise FileValidationError(f"File is empty: {file_path}")
        
        if file_size > self.MAX_FILE_SIZE:
            raise FileValidationError(
                f"File too large: {file_size} bytes. "
                f"Maximum allowed: {self.MAX_FILE_SIZE} bytes"
            )
        
        # Check if file is readable
        if not os.access(file_path, os.R_OK):
            raise FileValidationError(f"File is not readable: {file_path}")
        
        return file_path
    
    def _validate_bytesio_file(self, file_obj: io.BytesIO) -> io.BytesIO:
        """
        Validate a BytesIO file object.
        
        Args:
            file_obj: BytesIO object
            
        Returns:
            Validated BytesIO object
            
        Raises:
            FileValidationError: If validation fails
        """
        # Check if object has name attribute
        if not hasattr(file_obj, 'name'):
            raise FileValidationError("BytesIO object must have a 'name' attribute")
        
        # Check file extension from name
        ext = os.path.splitext(file_obj.name)[1].lower()
        if ext not in self.SUPPORTED_EXTENSIONS:
            raise FileValidationError(
                f"Unsupported file extension: {ext}. "
                f"Supported extensions: {', '.join(self.SUPPORTED_EXTENSIONS)}"
            )
        
        # Check if BytesIO is empty
        file_obj.seek(0, os.SEEK_END)
        size = file_obj.tell()
        file_obj.seek(0)
        
        if size == 0:
            raise FileValidationError("BytesIO object is empty")
        
        if size > self.MAX_FILE_SIZE:
            raise FileValidationError(
                f"BytesIO object too large: {size} bytes. "
                f"Maximum allowed: {self.MAX_FILE_SIZE} bytes"
            )
        
        return file_obj
    
    @staticmethod
    def is_supported_file(file_path: Union[str, Path]) -> bool:
        """
        Check if a file extension is supported.
        
        Args:
            file_path: Path to file
            
        Returns:
            True if supported, False otherwise
        """
        ext = os.path.splitext(str(file_path))[1].lower()
        return ext in ResumeParserService.SUPPORTED_EXTENSIONS


def parse_resume(
    file_path: Union[str, Path, io.BytesIO],
    skills_file: Optional[str] = None,
    custom_regex: Optional[str] = None
) -> Dict:
    """
    Convenience function to parse a resume file.
    
    Args:
        file_path: Path to resume file or BytesIO object
        skills_file: Optional path to custom skills CSV file
        custom_regex: Optional custom regex for mobile number extraction
        
    Returns:
        Dictionary containing parsed resume data
        
    Raises:
        FileValidationError: If file validation fails
        ResumeParsingError: If parsing fails
        
    Example:
        >>> from pyresparser.resume_parser_service import parse_resume
        >>> data = parse_resume('resume.pdf')
        >>> print(data['name'])
        'John Doe'
    """
    service = ResumeParserService(skills_file, custom_regex)
    return service.parse_resume(file_path)
