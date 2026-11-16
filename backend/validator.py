from io import BytesIO
from pathlib import Path
import zipfile

from pypdf import PdfReader


MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_DOCUMENTS = 20
MAX_PAGES = 2

ALLOWED_EXTENSIONS = {".pdf", ".docx"}


def validate_document(file_name: str, file_bytes: bytes):
    if not file_bytes:
        return False, f"{file_name} is empty."

    extension = Path(file_name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        return False, "Only PDF and DOCX files are supported."

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        return False, f"{file_name} exceeds the maximum file size of {MAX_FILE_SIZE_MB} MB."

    if extension == ".pdf":
        try:
            reader = PdfReader(BytesIO(file_bytes))

            if reader.is_encrypted:
                return False, "Password-protected PDFs are not supported."

            pages = len(reader.pages)
            if not pages:
                return False, f"{file_name} has no pages."
            if pages > MAX_PAGES:
                return False, f"{file_name} contains {pages} pages. Maximum allowed is {MAX_PAGES} pages."

        except Exception:
            return False, "The PDF is corrupted or invalid."

    if extension == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(file_bytes)) as docx_file:
                if "word/document.xml" not in docx_file.namelist():
                    return False, "The DOCX file is invalid."

        except zipfile.BadZipFile:
            return False, "The DOCX file is corrupted."
        return False, f"{file_name}: DOCX page count cannot be reliably verified without rendering. Please convert it to a PDF with at most 2 pages."

    return True, None
