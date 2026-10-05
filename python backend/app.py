from fileinput import filename
from io import BytesIO
import os
import re
import sys
import json
import shutil
import traceback
import pyodbc
from pypdf import PdfReader
from openai import AzureOpenAI
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import uvicorn
from dotenv import load_dotenv

from blob_helper import upload_pdf_to_blob, download_pdf_bytes, verify_blob_upload
import uuid
import tempfile

# Safe PyMuPDF import
try:
    import pymupdf as fitz
    PYMUPDF_AVAILABLE = True
    print("[OK] PyMuPDF successfully loaded as primary extraction & geometry engine.")
except ImportError:
    try:
        import fitz
        PYMUPDF_AVAILABLE = True
        print("[OK] fitz successfully loaded as primary extraction & geometry engine.")
    except ImportError:
        PYMUPDF_AVAILABLE = False
        print("[WARN] PyMuPDF (pymupdf/fitz) not found. Bounding box calculations will be skipped.")

load_dotenv()

# Force safe UTF-8 encoding behavior on Windows terminals
try:
    if sys.platform.startswith("win"):
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============================================================
# CONFIG
# ============================================================
AOAI_ENDPOINT = os.getenv("AOAI_ENDPOINT")
AOAI_KEY = os.getenv("AOAI_KEY")
AOAI_DEPLOYMENT = os.getenv("AOAI_DEPLOYMENT", "gpt-chat-latest")
AOAI_API_VERSION = os.getenv("AOAI_API_VERSION", "2024-04-01-preview")
SQL_CONNECTION_STRING = os.getenv("SQL_CONNECTION_STRING")

if not AOAI_ENDPOINT or not AOAI_KEY or not SQL_CONNECTION_STRING:
    raise RuntimeError("[Error] Critical environment variables are missing. Please check your .env configuration file.")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="PDF Chapter Comparison API",
    description="Backend API with AI and geometric bounding box coordinate mapping capabilities.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

llm_client = AzureOpenAI(
    azure_endpoint=AOAI_ENDPOINT,
    api_key=AOAI_KEY,
    api_version=AOAI_API_VERSION
)

import os
v1_path = os.path.join(UPLOAD_DIR, f"v1_{filename}")
print(f"File exists: {os.path.exists(v1_path)}, size: {os.path.getsize(v1_path) if os.path.exists(v1_path) else 'N/A'}")

def get_db():
    return pyodbc.connect(SQL_CONNECTION_STRING)

# ============================================================
# PYDANTIC MODELS
# ============================================================
class CompareSingleRequest(BaseModel):
    pdf_id_v1: int
    pdf_id_v2: int
    chapter_number: int

class CompareAllRequest(BaseModel):
    pdf_id_v1: int
    pdf_id_v2: int

# ============================================================
# COORDINATE EXTRACTION ENGINE (PyMuPDF / fitz)
# ============================================================
# def locate_text_in_pdf(pdf_path: str, search_text: str, start_page: int, end_page: int) -> Optional[dict]:
#     if not PYMUPDF_AVAILABLE or not search_text or not search_text.strip():
#         return None

#     try:
#         doc = fitz.open(pdf_path)
#         start_idx = max(0, start_page - 1)
#         end_idx = min(len(doc), end_page)

#         clean_search = " ".join(search_text.split())

#         # Stage 1: Exact Match in Chapter Window
#         for page_idx in range(start_idx, end_idx):
#             page = doc[page_idx]
#             rects = page.search_for(clean_search)
#             if rects:
#                 return {
#                     "page": page_idx + 1,
#                     "rects": [{"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1} for r in rects]
#                 }

#         # Stage 2: Fallback to Sub-string Matching (First 4 words)
#         words = clean_search.split()
#         if len(words) > 4:
#             fallback_search = " ".join(words[:4])
#             for page_idx in range(start_idx, end_idx):
#                 page = doc[page_idx]
#                 rects = page.search_for(fallback_search)
#                 if rects:
#                     return {
#                         "page": page_idx + 1,
#                         "rects": [{"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1} for r in rects]
#                     }

#         # Stage 3: Global PDF Scan
#         for page_idx in range(len(doc)):
#             page = doc[page_idx]
#             rects = page.search_for(clean_search)
#             if rects:
#                 return {
#                     "page": page_idx + 1,
#                     "rects": [{"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1} for r in rects]
#                 }
#     except Exception as e:
#         print(f"[WARN] Error locating coordinates: {e}")
#     return None

def normalize_dashes(text: str) -> str:
    # Replace all dash/hyphen variants with plain hyphen
    dash_chars = ['\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015', '\u2212']
    for dc in dash_chars:
        text = text.replace(dc, '-')
    return text

def locate_text_in_pdf(blob_name: str, search_text: str, start_page: int, end_page: int) -> Optional[dict]:
    if not PYMUPDF_AVAILABLE or not search_text or not search_text.strip() or not blob_name:
        return None
    try:
        pdf_bytes = download_pdf_bytes(blob_name)
        doc = fitz.open(stream=BytesIO(pdf_bytes), filetype="pdf")

        clean_text = " ".join(search_text.split())
        if not clean_text:
            return None

        # Stage 1: Exact continuous match
        for page_idx in range(len(doc)):
            rects = doc[page_idx].search_for(clean_text)
            if rects:
                return {"page": page_idx + 1, "rects": [{"x0": r.x0,"y0": r.y0,"x1": r.x1,"y1": r.y1} for r in rects]}

        # Stage 2: Smart chunking (multi-line wrapped sentences)
        words = clean_text.split()
        if len(words) >= 2:
            chunk_size = 4
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                page_rects = []
                for i in range(0, len(words), 3):
                    chunk = " ".join(words[i:i + chunk_size])
                    if len(chunk) < 3:
                        continue
                    rects = page.search_for(chunk)
                    if rects:
                        page_rects.extend(rects)
                if page_rects:
                    unique_rects = []
                    for r in page_rects:
                        r_dict = {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1}
                        if r_dict not in unique_rects:
                            unique_rects.append(r_dict)
                    return {"page": page_idx + 1, "rects": unique_rects}

        # Stage 3: Digits/link-annotation fallback (phone, fax numbers)
        digits_search = re.sub(r'[^\d]', '', clean_text)
        if len(digits_search) >= 7:
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                for link in page.get_links():
                    uri_digits = re.sub(r'[^\d]', '', link.get('uri', '') or '')
                    if digits_search in uri_digits or uri_digits in digits_search:
                        rect = link['from']
                        return {"page": page_idx + 1, "rects": [{"x0": rect.x0,"y0": rect.y0,"x1": rect.x1,"y1": rect.y1}]}

    except Exception as e:
        print(f"[WARN] Error locating coordinates from blob '{blob_name}': {e}")
    return None
# ============================================================
# METADATA & PARSING FLOWS
# ============================================================
NUM_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'i': 1, 'ii': 2,
    'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10
}

def parse_chapter_number(val_str: str) -> Optional[int]:
    val_str = val_str.strip().lower()
    if val_str.isdigit():
        return int(val_str)
    return NUM_WORDS.get(val_str, None)

def detect_chapters_from_toc(pdf_path: str):
    if PYMUPDF_AVAILABLE:
        try:
            doc = fitz.open(pdf_path)
            bookmarks = doc.get_toc()
            chapters = []
            if bookmarks:
                for b in bookmarks:
                    level, title, page = b[0], b[1], b[2]
                    match = re.search(r'\b(?:CHAPTER|CHAP|SECTION)\s+([0-9]+|[A-Za-z]+)\b[:\.\-\s]*(.*)', title, re.IGNORECASE)
                    if match:
                        ch_num = parse_chapter_number(match.group(1))
                        if ch_num is not None:
                            chapters.append({"chapter": ch_num, "title": title.strip(), "toc_page": page})
            if len(chapters) >= 2:
                unique = {c["chapter"]: c for c in chapters}
                return sorted(list(unique.values()), key=lambda x: x["chapter"])
        except Exception as e:
            print(f"[WARN] Fitz TOC extraction warning: {e}")

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    chapters_dict = {}
    scan_limit = min(20, total_pages)
    toc_raw_lines = []
    for p_no in range(scan_limit):
        txt = reader.pages[p_no].extract_text() or ""
        for line in txt.split('\n'):
            line = line.strip()
            if line: toc_raw_lines.append(line)

    for idx, line in enumerate(toc_raw_lines):
        ch_match = re.search(r'\b(?:CHAPTER|CHAP|SECTION)\s+([0-9]+|[A-Za-z]+)\b[:\.\-\s]*(.*)', line, re.IGNORECASE)
        if ch_match:
            ch_num = parse_chapter_number(ch_match.group(1))
            if ch_num is None or ch_num in chapters_dict: continue

            title_parts = [ch_match.group(2).strip()] if ch_match.group(2) else []
            found_page = None

            inline_page = re.search(r'(\d+)\s*$', line)
            if inline_page and inline_page.group(1) != str(ch_match.group(1)):
                found_page = int(inline_page.group(1))

            if not found_page:
                for lookahead in range(1, 4):
                    if idx + lookahead >= len(toc_raw_lines): break
                    next_line = toc_raw_lines[idx + lookahead].strip()
                    if re.search(r'\bCHAPTER\s+([0-9]+|[A-Za-z]+)\b', next_line, re.IGNORECASE): break
                    page_match = re.search(r'(?:^|[\.\s_-])(\d+)\s*$', next_line)
                    if page_match:
                        found_page = int(page_match.group(1))
                        clean_title_part = re.sub(r'[\.\-_…\d]+$', '', next_line).strip()
                        if clean_title_part: title_parts.append(clean_title_part)
                        break
                    else:
                        title_parts.append(next_line)

            full_title = f"CHAPTER {ch_num}"
            cleaned_title_text = " ".join([p for p in title_parts if p]).strip()
            cleaned_title_text = re.sub(r'[\.\-_…\s]+$', '', cleaned_title_text).strip()
            if cleaned_title_text: full_title += f": {cleaned_title_text}"

            if found_page:
                chapters_dict[ch_num] = {"chapter": ch_num, "title": full_title, "toc_page": found_page}

    if len(chapters_dict) < 3:
        for p_idx in range(total_pages):
            page_text = (reader.pages[p_idx].extract_text() or "").strip()
            if not page_text: continue
            header_text = page_text[:500]
            header_match = re.search(r'\bCHAPTER\s+([0-9]+|[A-Za-z]+)\b[:\.\-\s]*(.*)', header_text, re.IGNORECASE)
            if header_match:
                ch_num = parse_chapter_number(header_match.group(1))
                if ch_num is not None and ch_num not in chapters_dict:
                    title_line = header_match.group(2).strip().split('\n')[0]
                    title_line = re.sub(r'[\.\-_…]+$', '', title_line).strip()
                    full_title = f"CHAPTER {ch_num}"
                    if title_line: full_title += f": {title_line}"
                    chapters_dict[ch_num] = {"chapter": ch_num, "title": full_title, "toc_page": p_idx + 1}

    final_chapters = list(chapters_dict.values())
    final_chapters.sort(key=lambda x: x["chapter"])
    return final_chapters

def extract_chapter_content(pdf_path: str, chapters: list):
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    chapters_with_content = []

    if not chapters:
        full_text = "".join([(p.extract_text() or "") for p in reader.pages])
        return [{"chapter": 1, "title": "Full Document", "start_page": 1, "end_page": total_pages, "content": full_text.strip(), "word_count": len(full_text.split())}]

    resolved_chapters = []
    for ch in chapters:
        target_ch = ch["chapter"]
        reported_page = ch["toc_page"]
        actual_page = None
        search_start = max(0, reported_page - 6)
        search_end = min(total_pages, reported_page + 15)

        for p_no in range(search_start, search_end):
            p_text = (reader.pages[p_no].extract_text() or "")[:600]
            if re.search(rf'\bCHAPTER\s+{target_ch}\b', p_text, re.IGNORECASE):
                actual_page = p_no + 1
                break

        if not actual_page:
            actual_page = max(1, min(reported_page, total_pages))

        resolved_chapters.append({"chapter": ch["chapter"], "title": ch["title"], "start_page": actual_page})

    for idx, ch in enumerate(resolved_chapters):
        start_page = ch["start_page"]
        if idx + 1 < len(resolved_chapters):
            next_start = resolved_chapters[idx + 1]["start_page"]
            end_page = next_start - 1 if next_start > start_page else start_page
        else:
            end_page = total_pages

        end_page = min(end_page, total_pages)
        full_text = ""
        for p in range(start_page, end_page + 1):
            page_index = p - 1
            if 0 <= page_index < total_pages:
                full_text += (reader.pages[page_index].extract_text() or "") + "\n"

        chapters_with_content.append({
            "chapter": ch["chapter"],
            "title": ch["title"],
            "start_page": start_page,
            "end_page": end_page,
            "content": full_text.strip(),
            "word_count": len(full_text.split())
        })

    return chapters_with_content

def save_pdf_and_chapters_to_db(pdf_name: str, pdf_version: str, chapters_data: list, total_pages: int, blob_name: str):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO pdf_documents (pdf_name, pdf_version, total_chapters, total_pages, blob_name)
            VALUES (?, ?, ?, ?, ?)
        """, pdf_name, pdf_version, len(chapters_data), total_pages, blob_name)
        cursor.execute("SELECT @@IDENTITY")
        pdf_id = int(cursor.fetchone()[0])

        for ch in chapters_data:
            cursor.execute("""
                INSERT INTO pdf_chapters (pdf_id, chapter_number, chapter_title, start_page, end_page, content, word_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, pdf_id, ch["chapter"], ch["title"], ch["start_page"], ch["end_page"], ch["content"], ch["word_count"])
        conn.commit()
        return pdf_id
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# ============================================================
# AZURE OPENAI COMPARISON (TEMPERATURE ERROR FIXED)
# ============================================================
def run_llm_comparison(pdf_id_v1: int, pdf_id_v2: int, chapter_number: int):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT chapter_title, content, start_page, end_page FROM pdf_chapters WHERE pdf_id = ? AND chapter_number = ?", pdf_id_v1, chapter_number)
    row_v1 = cursor.fetchone()

    cursor.execute("SELECT chapter_title, content, start_page, end_page FROM pdf_chapters WHERE pdf_id = ? AND chapter_number = ?", pdf_id_v2, chapter_number)
    row_v2 = cursor.fetchone()

    cursor.execute("SELECT pdf_name, blob_name FROM pdf_documents WHERE id = ?", pdf_id_v1)
    doc_v1_row = cursor.fetchone()

    cursor.execute("SELECT pdf_name, blob_name FROM pdf_documents WHERE id = ?", pdf_id_v2)
    doc_v2_row = cursor.fetchone()
    conn.close()

    if not row_v1 or not row_v2 or not doc_v1_row or not doc_v2_row:
        return None

    v1_title, v1_content, v1_start, v1_end = row_v1[0], row_v1[1][:15000], row_v1[2], row_v1[3]
    v2_title, v2_content, v2_start, v2_end = row_v2[0], row_v2[1][:15000], row_v2[2], row_v2[3]

    v1_blob_name = doc_v1_row[1]
    v2_blob_name = doc_v2_row[1]

    if not v1_blob_name or not v2_blob_name:
        print(f"[WARN] Missing blob_name for pdf_id_v1={pdf_id_v1} or pdf_id_v2={pdf_id_v2}. "
              f"Coordinates will not be located. Re-upload these PDFs via /api/pdf/process to populate blob_name.")

    prompt = f"""Compare these two versions of the same chapter:
## Chapter: {v1_title}

### VERSION 1 Content:
{v1_content}

### VERSION 2 Content:
{v2_content}

### Response Requirements:
You MUST respond with a single, valid JSON object containing exactly two keys:
1. "summary_markdown": A comprehensive overview of changes, additions, removals, and member impact.
2. "discrepancies": An array of specific textual changes. Each discrepancy MUST match this schema:
   {{
     "topic": "Short title label of the change (e.g., 'Copay Increase')",
     "change_type": "modified" | "added" | "deleted",
     "v1_text": "The EXACT substring from VERSION 1 that was modified/deleted. Leave blank if added.",
     "v2_text": "The EXACT substring from VERSION 2 that was modified/added. Leave blank if deleted."
   }}

Keep v1_text and v2_text matches highly faithful to original text spacing and capitalization for precise coordinate search.
"""

    response = llm_client.chat.completions.create(
        model=AOAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": "You are an expert compliance auditor. You output strict JSON structures matching the requested schemas."},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"}
    )

    raw_json = response.choices[0].message.content
    try:
        parsed_data = json.loads(raw_json)
    except Exception:
        parsed_data = {"summary_markdown": raw_json, "discrepancies": []}

    discrepancies = parsed_data.get("discrepancies", [])
    summary_markdown = parsed_data.get("summary_markdown", "")

    processed_discrepancies = []
    for disc in discrepancies:
        v1_text = disc.get("v1_text", "")
        v2_text = disc.get("v2_text", "")

        v1_coords = locate_text_in_pdf(v1_blob_name, v1_text, v1_start, v1_end) if v1_text and v1_blob_name else None
        v2_coords = locate_text_in_pdf(v2_blob_name, v2_text, v2_start, v2_end) if v2_text and v2_blob_name else None

        processed_discrepancies.append({
            "topic": disc.get("topic", "Structural Change"),
            "change_type": disc.get("change_type", "modified"),
            "v1_text": v1_text,
            "v2_text": v2_text,
            "v1_location": v1_coords,
            "v2_location": v2_coords
        })

    final_payload = {
        "summary": summary_markdown,
        "discrepancies": processed_discrepancies
    }
    serialized_payload = json.dumps(final_payload, ensure_ascii=False)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id FROM chapter_comparisons 
        WHERE pdf_id_v1 = ? AND pdf_id_v2 = ? AND chapter_number = ?
    """, pdf_id_v1, pdf_id_v2, chapter_number)
    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE chapter_comparisons 
            SET v1_chapter_title = ?, v2_chapter_title = ?, llm_response = ?, comparison_date = CURRENT_TIMESTAMP
            WHERE id = ?
        """, v1_title, v2_title, serialized_payload, existing[0])
    else:
        cursor.execute("""
            INSERT INTO chapter_comparisons (chapter_number, pdf_id_v1, pdf_id_v2, v1_chapter_title, v2_chapter_title, llm_response)
            VALUES (?, ?, ?, ?, ?, ?)
        """, chapter_number, pdf_id_v1, pdf_id_v2, v1_title, v2_title, serialized_payload)
    conn.commit()
    conn.close()

    return {
        "chapter_number": chapter_number,
        "v1_title": v1_title,
        "v2_title": v2_title,
        "summary": summary_markdown,
        "discrepancies": processed_discrepancies
    }

def unpack_comparison_payload(response_str: str) -> dict:
    if not response_str:
        return {"summary": "No comparison log found.", "discrepancies": []}
    try:
        data = json.loads(response_str)
        if isinstance(data, dict) and ("summary" in data or "discrepancies" in data):
            return {
                "summary": data.get("summary", ""),
                "discrepancies": data.get("discrepancies", [])
            }
    except Exception:
        pass
    return {"summary": response_str, "discrepancies": []}

# ============================================================
# API ENDPOINTS
# ============================================================
@app.get("/api/health")
def health_check():
    return {"status": "healthy", "service": "PDF Geometric Comparison Engine"}

@app.post("/api/pdf/process")
async def process_pdfs_api(
    pdf_v1: UploadFile = File(...),
    pdf_v2: UploadFile = File(...),
    doc_name: str = Form("EOC Medicare Advantage"),
    version_v1: str = Form("2024"),
    version_v2: str = Form("2025")
):
    try:
        v1_filename = pdf_v1.filename or "v1.pdf"
        v2_filename = pdf_v2.filename or "v2.pdf"

        v1_bytes = await pdf_v1.read()
        v2_bytes = await pdf_v2.read()

        v1_blob_name = f"{uuid.uuid4()}_v1_{v1_filename}"
        v2_blob_name = f"{uuid.uuid4()}_v2_{v2_filename}"

        upload_pdf_to_blob(v1_blob_name, v1_bytes)
        upload_pdf_to_blob(v2_blob_name, v2_bytes)

        v1_verification = verify_blob_upload(v1_blob_name)
        v2_verification = verify_blob_upload(v2_blob_name)

        print("[BLOB VERIFY] V1:", v1_verification)
        print("[BLOB VERIFY] V2:", v2_verification)

        # temp local files only for chapter-detection pass (deleted right after)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp1:
            tmp1.write(v1_bytes)
            v1_tmp_path = tmp1.name
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp2:
            tmp2.write(v2_bytes)
            v2_tmp_path = tmp2.name

        try:
            r_v1 = PdfReader(v1_tmp_path)
            ch_v1 = detect_chapters_from_toc(v1_tmp_path)
            content_v1 = extract_chapter_content(v1_tmp_path, ch_v1)
            pdf_id_v1 = save_pdf_and_chapters_to_db(v1_filename, version_v1, content_v1, len(r_v1.pages), v1_blob_name)

            r_v2 = PdfReader(v2_tmp_path)
            ch_v2 = detect_chapters_from_toc(v2_tmp_path)
            content_v2 = extract_chapter_content(v2_tmp_path, ch_v2)
            pdf_id_v2 = save_pdf_and_chapters_to_db(v2_filename, version_v2, content_v2, len(r_v2.pages), v2_blob_name)
        finally:
            os.unlink(v1_tmp_path)
            os.unlink(v2_tmp_path)

        return {
            "success": True,
            "message": "Both PDFs uploaded to blob storage, parsed, and saved.",
            "pdf_id_v1": pdf_id_v1,
            "pdf_id_v2": pdf_id_v2,
            "chapters_v1_count": len(content_v1),
            "chapters_v2_count": len(content_v2)
        }
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

    
@app.post("/api/pdf/compare-chapter")
def compare_single_chapter_api(req: CompareSingleRequest):
    try:
        result = run_llm_comparison(req.pdf_id_v1, req.pdf_id_v2, req.chapter_number)
        if not result:
            raise HTTPException(status_code=404, detail="Chapter contents not found")
        return {"success": True, "data": result}
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

@app.post("/api/pdf/compare-all")
def compare_all_chapters_api(req: CompareAllRequest):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT chapter_number FROM pdf_chapters WHERE pdf_id IN (?, ?) ORDER BY chapter_number", int(req.pdf_id_v1), int(req.pdf_id_v2))
        ch_nums = [row[0] for row in cursor.fetchall()]
        conn.close()

        print(f"[INFO] Comparing all {len(ch_nums)} chapters via Azure OpenAI...")
        results = []
        for num in ch_nums:
            print(f"[INFO] Processing chapter {num}...")
            comp = run_llm_comparison(req.pdf_id_v1, req.pdf_id_v2, num)
            if comp:
                results.append(comp)

        return {"success": True, "total_compared": len(results), "results": results}
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

@app.get("/api/pdf/comparisons/{pdf_id_v1}/{pdf_id_v2}")
def get_stored_comparisons(pdf_id_v1: int, pdf_id_v2: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT chapter_number, v1_chapter_title, v2_chapter_title, llm_response, comparison_date
        FROM chapter_comparisons
        WHERE pdf_id_v1 = ? AND pdf_id_v2 = ?
        ORDER BY chapter_number
    """, pdf_id_v1, pdf_id_v2)
    rows = cursor.fetchall()
    conn.close()

    data = []
    for r in rows:
        unpacked = unpack_comparison_payload(r[3])
        data.append({
            "chapter_number": r[0],
            "v1_title": r[1],
            "v2_title": r[2],
            "summary": unpacked["summary"],
            "discrepancies": unpacked["discrepancies"],
            "comparison_date": str(r[4])
        })
    return {"success": True, "comparisons": data}

@app.get("/api/pdf/history")
def get_all_comparison_history():
    try:
        conn = get_db()
        cursor = conn.cursor()
        query = """
            SELECT cc.pdf_id_v1, cc.pdf_id_v2, v1.pdf_name, v1.pdf_version, v2.pdf_name, v2.pdf_version,
                   COUNT(DISTINCT cc.chapter_number) AS total_chapters, MIN(cc.comparison_date), MAX(cc.comparison_date)
            FROM chapter_comparisons cc
            INNER JOIN pdf_documents v1 ON cc.pdf_id_v1 = v1.id
            INNER JOIN pdf_documents v2 ON cc.pdf_id_v2 = v2.id
            GROUP BY cc.pdf_id_v1, cc.pdf_id_v2, v1.pdf_name, v1.pdf_version, v2.pdf_name, v2.pdf_version
            ORDER BY MAX(cc.comparison_date) DESC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()

        history = [{
            "pdf_id_v1": r[0], "pdf_id_v2": r[1], "v1_name": r[2], "v1_version": r[3],
            "v2_name": r[4], "v2_version": r[5], "total_chapters_compared": r[6],
            "first_compared": str(r[7]), "last_compared": str(r[8])
        } for r in rows]

        return {"success": True, "total_sessions": len(history), "history": history}
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

@app.get("/api/pdf/history/{pdf_id_v1}/{pdf_id_v2}")
def get_comparison_detail(pdf_id_v1: int, pdf_id_v2: int):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT pdf_name, pdf_version FROM pdf_documents WHERE id = ?", int(pdf_id_v1))
        v1_info = cursor.fetchone()
        cursor.execute("SELECT pdf_name, pdf_version FROM pdf_documents WHERE id = ?", int(pdf_id_v2))
        v2_info = cursor.fetchone()

        cursor.execute("""
            SELECT chapter_number, v1_chapter_title, v2_chapter_title, llm_response, comparison_date
            FROM chapter_comparisons WHERE pdf_id_v1 = ? AND pdf_id_v2 = ? ORDER BY chapter_number
        """, int(pdf_id_v1), int(pdf_id_v2))
        rows = cursor.fetchall()
        conn.close()

        chapters = []
        for r in rows:
            unpacked = unpack_comparison_payload(r[3])
            chapters.append({
                "chapter_number": r[0],
                "v1_title": r[1],
                "v2_title": r[2],
                "summary": unpacked["summary"],
                "discrepancies": unpacked["discrepancies"],
                "comparison_date": str(r[4])
            })

        return {
            "success": True, "pdf_id_v1": pdf_id_v1, "pdf_id_v2": pdf_id_v2,
            "v1_name": v1_info[0] if v1_info else "V1", "v1_version": v1_info[1] if v1_info else "",
            "v2_name": v2_info[0] if v2_info else "V2", "v2_version": v2_info[1] if v2_info else "",
            "total_chapters": len(chapters), "chapters": chapters
        }
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

# ============================================================
# DYNAMIC SERVER RUNNER
# ============================================================
from fileinput import filename
from io import BytesIO
import os
import re
import sys
import json
import shutil
import traceback
import pyodbc
from pypdf import PdfReader
from openai import AzureOpenAI
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import uvicorn
from dotenv import load_dotenv

from blob_helper import upload_pdf_to_blob, download_pdf_bytes
import uuid
import tempfile

# Safe PyMuPDF import
try:
    import pymupdf as fitz
    PYMUPDF_AVAILABLE = True
    print("[OK] PyMuPDF successfully loaded as primary extraction & geometry engine.")
except ImportError:
    try:
        import fitz
        PYMUPDF_AVAILABLE = True
        print("[OK] fitz successfully loaded as primary extraction & geometry engine.")
    except ImportError:
        PYMUPDF_AVAILABLE = False
        print("[WARN] PyMuPDF (pymupdf/fitz) not found. Bounding box calculations will be skipped.")

load_dotenv()

# Force safe UTF-8 encoding behavior on Windows terminals
try:
    if sys.platform.startswith("win"):
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============================================================
# CONFIG
# ============================================================
AOAI_ENDPOINT = os.getenv("AOAI_ENDPOINT")
AOAI_KEY = os.getenv("AOAI_KEY")
AOAI_DEPLOYMENT = os.getenv("AOAI_DEPLOYMENT", "gpt-chat-latest")
AOAI_API_VERSION = os.getenv("AOAI_API_VERSION", "2024-04-01-preview")
SQL_CONNECTION_STRING = os.getenv("SQL_CONNECTION_STRING")

if not AOAI_ENDPOINT or not AOAI_KEY or not SQL_CONNECTION_STRING:
    raise RuntimeError("[Error] Critical environment variables are missing. Please check your .env configuration file.")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="PDF Chapter Comparison API",
    description="Backend API with AI and geometric bounding box coordinate mapping capabilities.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

llm_client = AzureOpenAI(
    azure_endpoint=AOAI_ENDPOINT,
    api_key=AOAI_KEY,
    api_version=AOAI_API_VERSION
)

import os
v1_path = os.path.join(UPLOAD_DIR, f"v1_{filename}")
print(f"File exists: {os.path.exists(v1_path)}, size: {os.path.getsize(v1_path) if os.path.exists(v1_path) else 'N/A'}")

def get_db():
    return pyodbc.connect(SQL_CONNECTION_STRING)

# ============================================================
# PYDANTIC MODELS
# ============================================================
class CompareSingleRequest(BaseModel):
    pdf_id_v1: int
    pdf_id_v2: int
    chapter_number: int

class CompareAllRequest(BaseModel):
    pdf_id_v1: int
    pdf_id_v2: int

# ============================================================
# COORDINATE EXTRACTION ENGINE (PyMuPDF / fitz)
# ============================================================
# def locate_text_in_pdf(pdf_path: str, search_text: str, start_page: int, end_page: int) -> Optional[dict]:
#     if not PYMUPDF_AVAILABLE or not search_text or not search_text.strip():
#         return None

#     try:
#         doc = fitz.open(pdf_path)
#         start_idx = max(0, start_page - 1)
#         end_idx = min(len(doc), end_page)

#         clean_search = " ".join(search_text.split())

#         # Stage 1: Exact Match in Chapter Window
#         for page_idx in range(start_idx, end_idx):
#             page = doc[page_idx]
#             rects = page.search_for(clean_search)
#             if rects:
#                 return {
#                     "page": page_idx + 1,
#                     "rects": [{"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1} for r in rects]
#                 }

#         # Stage 2: Fallback to Sub-string Matching (First 4 words)
#         words = clean_search.split()
#         if len(words) > 4:
#             fallback_search = " ".join(words[:4])
#             for page_idx in range(start_idx, end_idx):
#                 page = doc[page_idx]
#                 rects = page.search_for(fallback_search)
#                 if rects:
#                     return {
#                         "page": page_idx + 1,
#                         "rects": [{"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1} for r in rects]
#                     }

#         # Stage 3: Global PDF Scan
#         for page_idx in range(len(doc)):
#             page = doc[page_idx]
#             rects = page.search_for(clean_search)
#             if rects:
#                 return {
#                     "page": page_idx + 1,
#                     "rects": [{"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1} for r in rects]
#                 }
#     except Exception as e:
#         print(f"[WARN] Error locating coordinates: {e}")
#     return None

def normalize_dashes(text: str) -> str:
    # Replace all dash/hyphen variants with plain hyphen
    dash_chars = ['\u2010', '\u2011', '\u2012', '\u2013', '\u2014', '\u2015', '\u2212']
    for dc in dash_chars:
        text = text.replace(dc, '-')
    return text

def locate_text_in_pdf(blob_name: str, search_text: str, start_page: int, end_page: int) -> Optional[dict]:
    if not PYMUPDF_AVAILABLE or not search_text or not search_text.strip() or not blob_name:
        return None
    try:
        pdf_bytes = download_pdf_bytes(blob_name)
        doc = fitz.open(stream=BytesIO(pdf_bytes), filetype="pdf")

        clean_text = " ".join(search_text.split())
        if not clean_text:
            return None

        # Stage 1: Exact continuous match
        for page_idx in range(len(doc)):
            rects = doc[page_idx].search_for(clean_text)
            if rects:
                return {"page": page_idx + 1, "rects": [{"x0": r.x0,"y0": r.y0,"x1": r.x1,"y1": r.y1} for r in rects]}

        # Stage 2: Smart chunking (multi-line wrapped sentences)
        words = clean_text.split()
        if len(words) >= 2:
            chunk_size = 4
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                page_rects = []
                for i in range(0, len(words), 3):
                    chunk = " ".join(words[i:i + chunk_size])
                    if len(chunk) < 3:
                        continue
                    rects = page.search_for(chunk)
                    if rects:
                        page_rects.extend(rects)
                if page_rects:
                    unique_rects = []
                    for r in page_rects:
                        r_dict = {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1}
                        if r_dict not in unique_rects:
                            unique_rects.append(r_dict)
                    return {"page": page_idx + 1, "rects": unique_rects}

        # Stage 3: Digits/link-annotation fallback (phone, fax numbers)
        digits_search = re.sub(r'[^\d]', '', clean_text)
        if len(digits_search) >= 7:
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                for link in page.get_links():
                    uri_digits = re.sub(r'[^\d]', '', link.get('uri', '') or '')
                    if digits_search in uri_digits or uri_digits in digits_search:
                        rect = link['from']
                        return {"page": page_idx + 1, "rects": [{"x0": rect.x0,"y0": rect.y0,"x1": rect.x1,"y1": rect.y1}]}

    except Exception as e:
        print(f"[WARN] Error locating coordinates from blob '{blob_name}': {e}")
    return None
# ============================================================
# METADATA & PARSING FLOWS
# ============================================================
NUM_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'i': 1, 'ii': 2,
    'iii': 3, 'iv': 4, 'v': 5, 'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10
}

def parse_chapter_number(val_str: str) -> Optional[int]:
    val_str = val_str.strip().lower()
    if val_str.isdigit():
        return int(val_str)
    return NUM_WORDS.get(val_str, None)

def detect_chapters_from_toc(pdf_path: str):
    if PYMUPDF_AVAILABLE:
        try:
            doc = fitz.open(pdf_path)
            bookmarks = doc.get_toc()
            chapters = []
            if bookmarks:
                for b in bookmarks:
                    level, title, page = b[0], b[1], b[2]
                    match = re.search(r'\b(?:CHAPTER|CHAP|SECTION)\s+([0-9]+|[A-Za-z]+)\b[:\.\-\s]*(.*)', title, re.IGNORECASE)
                    if match:
                        ch_num = parse_chapter_number(match.group(1))
                        if ch_num is not None:
                            chapters.append({"chapter": ch_num, "title": title.strip(), "toc_page": page})
            if len(chapters) >= 2:
                unique = {c["chapter"]: c for c in chapters}
                return sorted(list(unique.values()), key=lambda x: x["chapter"])
        except Exception as e:
            print(f"[WARN] Fitz TOC extraction warning: {e}")

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    chapters_dict = {}
    scan_limit = min(20, total_pages)
    toc_raw_lines = []
    for p_no in range(scan_limit):
        txt = reader.pages[p_no].extract_text() or ""
        for line in txt.split('\n'):
            line = line.strip()
            if line: toc_raw_lines.append(line)

    for idx, line in enumerate(toc_raw_lines):
        ch_match = re.search(r'\b(?:CHAPTER|CHAP|SECTION)\s+([0-9]+|[A-Za-z]+)\b[:\.\-\s]*(.*)', line, re.IGNORECASE)
        if ch_match:
            ch_num = parse_chapter_number(ch_match.group(1))
            if ch_num is None or ch_num in chapters_dict: continue

            title_parts = [ch_match.group(2).strip()] if ch_match.group(2) else []
            found_page = None

            inline_page = re.search(r'(\d+)\s*$', line)
            if inline_page and inline_page.group(1) != str(ch_match.group(1)):
                found_page = int(inline_page.group(1))

            if not found_page:
                for lookahead in range(1, 4):
                    if idx + lookahead >= len(toc_raw_lines): break
                    next_line = toc_raw_lines[idx + lookahead].strip()
                    if re.search(r'\bCHAPTER\s+([0-9]+|[A-Za-z]+)\b', next_line, re.IGNORECASE): break
                    page_match = re.search(r'(?:^|[\.\s_-])(\d+)\s*$', next_line)
                    if page_match:
                        found_page = int(page_match.group(1))
                        clean_title_part = re.sub(r'[\.\-_…\d]+$', '', next_line).strip()
                        if clean_title_part: title_parts.append(clean_title_part)
                        break
                    else:
                        title_parts.append(next_line)

            full_title = f"CHAPTER {ch_num}"
            cleaned_title_text = " ".join([p for p in title_parts if p]).strip()
            cleaned_title_text = re.sub(r'[\.\-_…\s]+$', '', cleaned_title_text).strip()
            if cleaned_title_text: full_title += f": {cleaned_title_text}"

            if found_page:
                chapters_dict[ch_num] = {"chapter": ch_num, "title": full_title, "toc_page": found_page}

    if len(chapters_dict) < 3:
        for p_idx in range(total_pages):
            page_text = (reader.pages[p_idx].extract_text() or "").strip()
            if not page_text: continue
            header_text = page_text[:500]
            header_match = re.search(r'\bCHAPTER\s+([0-9]+|[A-Za-z]+)\b[:\.\-\s]*(.*)', header_text, re.IGNORECASE)
            if header_match:
                ch_num = parse_chapter_number(header_match.group(1))
                if ch_num is not None and ch_num not in chapters_dict:
                    title_line = header_match.group(2).strip().split('\n')[0]
                    title_line = re.sub(r'[\.\-_…]+$', '', title_line).strip()
                    full_title = f"CHAPTER {ch_num}"
                    if title_line: full_title += f": {title_line}"
                    chapters_dict[ch_num] = {"chapter": ch_num, "title": full_title, "toc_page": p_idx + 1}

    final_chapters = list(chapters_dict.values())
    final_chapters.sort(key=lambda x: x["chapter"])
    return final_chapters

def extract_chapter_content(pdf_path: str, chapters: list):
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    chapters_with_content = []

    if not chapters:
        full_text = "".join([(p.extract_text() or "") for p in reader.pages])
        return [{"chapter": 1, "title": "Full Document", "start_page": 1, "end_page": total_pages, "content": full_text.strip(), "word_count": len(full_text.split())}]

    resolved_chapters = []
    for ch in chapters:
        target_ch = ch["chapter"]
        reported_page = ch["toc_page"]
        actual_page = None
        search_start = max(0, reported_page - 6)
        search_end = min(total_pages, reported_page + 15)

        for p_no in range(search_start, search_end):
            p_text = (reader.pages[p_no].extract_text() or "")[:600]
            if re.search(rf'\bCHAPTER\s+{target_ch}\b', p_text, re.IGNORECASE):
                actual_page = p_no + 1
                break

        if not actual_page:
            actual_page = max(1, min(reported_page, total_pages))

        resolved_chapters.append({"chapter": ch["chapter"], "title": ch["title"], "start_page": actual_page})

    for idx, ch in enumerate(resolved_chapters):
        start_page = ch["start_page"]
        if idx + 1 < len(resolved_chapters):
            next_start = resolved_chapters[idx + 1]["start_page"]
            end_page = next_start - 1 if next_start > start_page else start_page
        else:
            end_page = total_pages

        end_page = min(end_page, total_pages)
        full_text = ""
        for p in range(start_page, end_page + 1):
            page_index = p - 1
            if 0 <= page_index < total_pages:
                full_text += (reader.pages[page_index].extract_text() or "") + "\n"

        chapters_with_content.append({
            "chapter": ch["chapter"],
            "title": ch["title"],
            "start_page": start_page,
            "end_page": end_page,
            "content": full_text.strip(),
            "word_count": len(full_text.split())
        })

    return chapters_with_content

def save_pdf_and_chapters_to_db(pdf_name: str, pdf_version: str, chapters_data: list, total_pages: int, blob_name: str):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO pdf_documents (pdf_name, pdf_version, total_chapters, total_pages, blob_name)
            VALUES (?, ?, ?, ?, ?)
        """, pdf_name, pdf_version, len(chapters_data), total_pages, blob_name)
        cursor.execute("SELECT @@IDENTITY")
        pdf_id = int(cursor.fetchone()[0])

        for ch in chapters_data:
            cursor.execute("""
                INSERT INTO pdf_chapters (pdf_id, chapter_number, chapter_title, start_page, end_page, content, word_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, pdf_id, ch["chapter"], ch["title"], ch["start_page"], ch["end_page"], ch["content"], ch["word_count"])
        conn.commit()
        return pdf_id
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# ============================================================
# AZURE OPENAI COMPARISON (TEMPERATURE ERROR FIXED)
# ============================================================
def run_llm_comparison(pdf_id_v1: int, pdf_id_v2: int, chapter_number: int):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT chapter_title, content, start_page, end_page FROM pdf_chapters WHERE pdf_id = ? AND chapter_number = ?", pdf_id_v1, chapter_number)
    row_v1 = cursor.fetchone()

    cursor.execute("SELECT chapter_title, content, start_page, end_page FROM pdf_chapters WHERE pdf_id = ? AND chapter_number = ?", pdf_id_v2, chapter_number)
    row_v2 = cursor.fetchone()

    cursor.execute("SELECT pdf_name, blob_name FROM pdf_documents WHERE id = ?", pdf_id_v1)
    doc_v1_row = cursor.fetchone()

    cursor.execute("SELECT pdf_name, blob_name FROM pdf_documents WHERE id = ?", pdf_id_v2)
    doc_v2_row = cursor.fetchone()
    conn.close()

    if not row_v1 or not row_v2 or not doc_v1_row or not doc_v2_row:
        return None

    v1_title, v1_content, v1_start, v1_end = row_v1[0], row_v1[1][:15000], row_v1[2], row_v1[3]
    v2_title, v2_content, v2_start, v2_end = row_v2[0], row_v2[1][:15000], row_v2[2], row_v2[3]

    v1_blob_name = doc_v1_row[1]
    v2_blob_name = doc_v2_row[1]

    if not v1_blob_name or not v2_blob_name:
        print(f"[WARN] Missing blob_name for pdf_id_v1={pdf_id_v1} or pdf_id_v2={pdf_id_v2}. "
              f"Coordinates will not be located. Re-upload these PDFs via /api/pdf/process to populate blob_name.")

    prompt = f"""Compare these two versions of the same chapter:
## Chapter: {v1_title}

### VERSION 1 Content:
{v1_content}

### VERSION 2 Content:
{v2_content}

### Response Requirements:
You MUST respond with a single, valid JSON object containing exactly two keys:
1. "summary_markdown": A comprehensive overview of changes, additions, removals, and member impact.
2. "discrepancies": An array of specific textual changes. Each discrepancy MUST match this schema:
   {{
     "topic": "Short title label of the change (e.g., 'Copay Increase')",
     "change_type": "modified" | "added" | "deleted",
     "v1_text": "The EXACT substring from VERSION 1 that was modified/deleted. Leave blank if added.",
     "v2_text": "The EXACT substring from VERSION 2 that was modified/added. Leave blank if deleted."
   }}

Keep v1_text and v2_text matches highly faithful to original text spacing and capitalization for precise coordinate search.
"""

    response = llm_client.chat.completions.create(
        model=AOAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": "You are an expert compliance auditor. You output strict JSON structures matching the requested schemas."},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"}
    )

    raw_json = response.choices[0].message.content
    try:
        parsed_data = json.loads(raw_json)
    except Exception:
        parsed_data = {"summary_markdown": raw_json, "discrepancies": []}

    discrepancies = parsed_data.get("discrepancies", [])
    summary_markdown = parsed_data.get("summary_markdown", "")

    processed_discrepancies = []
    for disc in discrepancies:
        v1_text = disc.get("v1_text", "")
        v2_text = disc.get("v2_text", "")

        v1_coords = locate_text_in_pdf(v1_blob_name, v1_text, v1_start, v1_end) if v1_text and v1_blob_name else None
        v2_coords = locate_text_in_pdf(v2_blob_name, v2_text, v2_start, v2_end) if v2_text and v2_blob_name else None

        processed_discrepancies.append({
            "topic": disc.get("topic", "Structural Change"),
            "change_type": disc.get("change_type", "modified"),
            "v1_text": v1_text,
            "v2_text": v2_text,
            "v1_location": v1_coords,
            "v2_location": v2_coords
        })

    final_payload = {
        "summary": summary_markdown,
        "discrepancies": processed_discrepancies
    }
    serialized_payload = json.dumps(final_payload, ensure_ascii=False)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id FROM chapter_comparisons 
        WHERE pdf_id_v1 = ? AND pdf_id_v2 = ? AND chapter_number = ?
    """, pdf_id_v1, pdf_id_v2, chapter_number)
    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE chapter_comparisons 
            SET v1_chapter_title = ?, v2_chapter_title = ?, llm_response = ?, comparison_date = CURRENT_TIMESTAMP
            WHERE id = ?
        """, v1_title, v2_title, serialized_payload, existing[0])
    else:
        cursor.execute("""
            INSERT INTO chapter_comparisons (chapter_number, pdf_id_v1, pdf_id_v2, v1_chapter_title, v2_chapter_title, llm_response)
            VALUES (?, ?, ?, ?, ?, ?)
        """, chapter_number, pdf_id_v1, pdf_id_v2, v1_title, v2_title, serialized_payload)
    conn.commit()
    conn.close()

    return {
        "chapter_number": chapter_number,
        "v1_title": v1_title,
        "v2_title": v2_title,
        "summary": summary_markdown,
        "discrepancies": processed_discrepancies
    }

def unpack_comparison_payload(response_str: str) -> dict:
    if not response_str:
        return {"summary": "No comparison log found.", "discrepancies": []}
    try:
        data = json.loads(response_str)
        if isinstance(data, dict) and ("summary" in data or "discrepancies" in data):
            return {
                "summary": data.get("summary", ""),
                "discrepancies": data.get("discrepancies", [])
            }
    except Exception:
        pass
    return {"summary": response_str, "discrepancies": []}

# ============================================================
# API ENDPOINTS
# ============================================================
@app.get("/api/health")
def health_check():
    return {"status": "healthy", "service": "PDF Geometric Comparison Engine"}

@app.post("/api/pdf/process")
async def process_pdfs_api(
    pdf_v1: UploadFile = File(...),
    pdf_v2: UploadFile = File(...),
    doc_name: str = Form("EOC Medicare Advantage"),
    version_v1: str = Form("2024"),
    version_v2: str = Form("2025")
):
    try:
        v1_filename = pdf_v1.filename or "v1.pdf"
        v2_filename = pdf_v2.filename or "v2.pdf"

        v1_bytes = await pdf_v1.read()
        v2_bytes = await pdf_v2.read()

        v1_blob_name = f"{uuid.uuid4()}_v1_{v1_filename}"
        v2_blob_name = f"{uuid.uuid4()}_v2_{v2_filename}"

        upload_pdf_to_blob(v1_blob_name, v1_bytes)
        upload_pdf_to_blob(v2_blob_name, v2_bytes)

        # temp local files only for chapter-detection pass (deleted right after)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp1:
            tmp1.write(v1_bytes)
            v1_tmp_path = tmp1.name
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp2:
            tmp2.write(v2_bytes)
            v2_tmp_path = tmp2.name

        try:
            r_v1 = PdfReader(v1_tmp_path)
            ch_v1 = detect_chapters_from_toc(v1_tmp_path)
            content_v1 = extract_chapter_content(v1_tmp_path, ch_v1)
            pdf_id_v1 = save_pdf_and_chapters_to_db(v1_filename, version_v1, content_v1, len(r_v1.pages), v1_blob_name)

            r_v2 = PdfReader(v2_tmp_path)
            ch_v2 = detect_chapters_from_toc(v2_tmp_path)
            content_v2 = extract_chapter_content(v2_tmp_path, ch_v2)
            pdf_id_v2 = save_pdf_and_chapters_to_db(v2_filename, version_v2, content_v2, len(r_v2.pages), v2_blob_name)
        finally:
            os.unlink(v1_tmp_path)
            os.unlink(v2_tmp_path)

        return {
            "success": True,
            "message": "Both PDFs uploaded to blob storage, parsed, and saved.",
            "pdf_id_v1": pdf_id_v1,
            "pdf_id_v2": pdf_id_v2,
            "chapters_v1_count": len(content_v1),
            "chapters_v2_count": len(content_v2)
        }
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

    
@app.post("/api/pdf/compare-chapter")
def compare_single_chapter_api(req: CompareSingleRequest):
    try:
        result = run_llm_comparison(req.pdf_id_v1, req.pdf_id_v2, req.chapter_number)
        if not result:
            raise HTTPException(status_code=404, detail="Chapter contents not found")
        return {"success": True, "data": result}
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

@app.post("/api/pdf/compare-all")
def compare_all_chapters_api(req: CompareAllRequest):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT chapter_number FROM pdf_chapters WHERE pdf_id IN (?, ?) ORDER BY chapter_number", int(req.pdf_id_v1), int(req.pdf_id_v2))
        ch_nums = [row[0] for row in cursor.fetchall()]
        conn.close()

        print(f"[INFO] Comparing all {len(ch_nums)} chapters via Azure OpenAI...")
        results = []
        for num in ch_nums:
            print(f"[INFO] Processing chapter {num}...")
            comp = run_llm_comparison(req.pdf_id_v1, req.pdf_id_v2, num)
            if comp:
                results.append(comp)

        return {"success": True, "total_compared": len(results), "results": results}
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

@app.get("/api/pdf/comparisons/{pdf_id_v1}/{pdf_id_v2}")
def get_stored_comparisons(pdf_id_v1: int, pdf_id_v2: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT chapter_number, v1_chapter_title, v2_chapter_title, llm_response, comparison_date
        FROM chapter_comparisons
        WHERE pdf_id_v1 = ? AND pdf_id_v2 = ?
        ORDER BY chapter_number
    """, pdf_id_v1, pdf_id_v2)
    rows = cursor.fetchall()
    conn.close()

    data = []
    for r in rows:
        unpacked = unpack_comparison_payload(r[3])
        data.append({
            "chapter_number": r[0],
            "v1_title": r[1],
            "v2_title": r[2],
            "summary": unpacked["summary"],
            "discrepancies": unpacked["discrepancies"],
            "comparison_date": str(r[4])
        })
    return {"success": True, "comparisons": data}

@app.get("/api/pdf/history")
def get_all_comparison_history():
    try:
        conn = get_db()
        cursor = conn.cursor()
        query = """
            SELECT cc.pdf_id_v1, cc.pdf_id_v2, v1.pdf_name, v1.pdf_version, v2.pdf_name, v2.pdf_version,
                   COUNT(DISTINCT cc.chapter_number) AS total_chapters, MIN(cc.comparison_date), MAX(cc.comparison_date)
            FROM chapter_comparisons cc
            INNER JOIN pdf_documents v1 ON cc.pdf_id_v1 = v1.id
            INNER JOIN pdf_documents v2 ON cc.pdf_id_v2 = v2.id
            GROUP BY cc.pdf_id_v1, cc.pdf_id_v2, v1.pdf_name, v1.pdf_version, v2.pdf_name, v2.pdf_version
            ORDER BY MAX(cc.comparison_date) DESC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()

        history = [{
            "pdf_id_v1": r[0], "pdf_id_v2": r[1], "v1_name": r[2], "v1_version": r[3],
            "v2_name": r[4], "v2_version": r[5], "total_chapters_compared": r[6],
            "first_compared": str(r[7]), "last_compared": str(r[8])
        } for r in rows]

        return {"success": True, "total_sessions": len(history), "history": history}
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

@app.get("/api/pdf/history/{pdf_id_v1}/{pdf_id_v2}")
def get_comparison_detail(pdf_id_v1: int, pdf_id_v2: int):
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT pdf_name, pdf_version FROM pdf_documents WHERE id = ?", int(pdf_id_v1))
        v1_info = cursor.fetchone()
        cursor.execute("SELECT pdf_name, pdf_version FROM pdf_documents WHERE id = ?", int(pdf_id_v2))
        v2_info = cursor.fetchone()

        cursor.execute("""
            SELECT chapter_number, v1_chapter_title, v2_chapter_title, llm_response, comparison_date
            FROM chapter_comparisons WHERE pdf_id_v1 = ? AND pdf_id_v2 = ? ORDER BY chapter_number
        """, int(pdf_id_v1), int(pdf_id_v2))
        rows = cursor.fetchall()
        conn.close()

        chapters = []
        for r in rows:
            unpacked = unpack_comparison_payload(r[3])
            chapters.append({
                "chapter_number": r[0],
                "v1_title": r[1],
                "v2_title": r[2],
                "summary": unpacked["summary"],
                "discrepancies": unpacked["discrepancies"],
                "comparison_date": str(r[4])
            })

        return {
            "success": True, "pdf_id_v1": pdf_id_v1, "pdf_id_v2": pdf_id_v2,
            "v1_name": v1_info[0] if v1_info else "V1", "v1_version": v1_info[1] if v1_info else "",
            "v2_name": v2_info[0] if v2_info else "V2", "v2_version": v2_info[1] if v2_info else "",
            "total_chapters": len(chapters), "chapters": chapters
        }
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(ex))

# ============================================================
# DYNAMIC SERVER RUNNER
# ============================================================
if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    
    module_name = os.path.splitext(os.path.basename(__file__))[0]
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=8000, reload=True)