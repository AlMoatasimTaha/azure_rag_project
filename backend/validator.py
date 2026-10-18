from io import BytesIO
from pathlib import Path
import zipfile

from pypdf import PdfReader


MAX_FILE_SIZE_MB = 10
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

ALLOWED_EXTENSIONS = {".pdf", ".docx"}


def validate_document(file_name: str, file_bytes: bytes):
    if not file_bytes:
        return False, "The file is empty."

    extension = Path(file_name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        return False, "Only PDF and DOCX files are supported."

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        return False, f"The file exceeds the {MAX_FILE_SIZE_MB} MB limit."

    if extension == ".pdf":
        try:
            reader = PdfReader(BytesIO(file_bytes))

            if reader.is_encrypted:
                return False, "Password-protected PDFs are not supported."

            _ = len(reader.pages)

        except Exception:
            return False, "The PDF is corrupted or invalid."

    if extension == ".docx":
        try:
            with zipfile.ZipFile(BytesIO(file_bytes)) as docx_file:
                if "word/document.xml" not in docx_file.namelist():
                    return False, "The DOCX file is invalid."

        except zipfile.BadZipFile:
            return False, "The DOCX file is corrupted."

    return True, None