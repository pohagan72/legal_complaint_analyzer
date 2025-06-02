from flask import Flask, request, render_template, send_file, flash
import os
import pandas as pd
import pdfplumber
from docx import Document
import google.generativeai as genai
from dotenv import load_dotenv
import json
import time
import traceback
import re
from waitress import serve  # Import Waitress

from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

app = Flask(__name__)
app.secret_key = 'your_very_secret_random_key_here_GEMINI_PRODUCTION_READY'  # IMPORTANT: Change this!
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# --- Google Gemini Client Initialization ---
gemini_model_global = None
gemini_api_key_global = os.getenv("GOOGLE_API_KEY")
gemini_model_name_global = os.getenv("GEMINI_MODEL")

if gemini_api_key_global and gemini_model_name_global:
    try:
        genai.configure(api_key=gemini_api_key_global)
        gemini_model_global = genai.GenerativeModel(gemini_model_name_global)
        print(f"Google Gemini Client Initialized. Model: {gemini_model_name_global}")
    except Exception as e:
        print(f"Error initializing Google Gemini client: {e}")
        gemini_model_global = None
else:
    print("Google Gemini API Key or Model Name not found. Gemini features will not work.")


def analyze_text_chunk_with_gemini(text_chunk, page_num_or_chunk_id, filename_for_context):
    """
    Analyzes a given text chunk using the Google Gemini model to extract legal allegations.
    Returns a list of dictionaries, each representing an allegation.
    """
    if not gemini_model_global:
        return [{"Error": "Google Gemini client not initialized."}]

    prompt_content = f"""
    You are a highly precise legal assistant analyzing a page/chunk of a legal complaint.
    Your absolute priority is to identify and extract **ALL specific, distinct allegations of anticompetitive conduct** that are *directly tied to a NAMED generic drug* found *within this page/chunk*.

    Your entire response MUST be a single JSON object with one top-level key: "allegations".
    The value of "allegations" MUST be a list of JSON objects. Each object in the list MUST represent ONE DISTINCT allegation related to a specific drug.

    Each allegation object MUST have these 6 keys:
    1.  "Product_Name": THIS IS MANDATORY. Provide the specific generic drug name(s) (and brand name in parentheses if available, e.g., "Carbamazepine ER (Tegretol XR)") that is the subject of THIS specific allegation. If multiple drugs are involved in the *same single distinct action*, list them comma-separated. If NO specific drug is mentioned in the immediate context of a general anticompetitive discussion, that discussion SHOULD NOT be included in the output. Every row MUST be tied to a specific drug.
    2.  "Allegation_Category": Categorize the primary anticompetitive conduct for THIS allegation (e.g., "Market Allocation", "Price Fixing", "Bid Rigging", "Information Exchange", "Refusal to Compete", "Fair Share Conspiracy", "Other Anticompetitive Conduct"). Choose the most fitting.
    3.  "Specific_Allegation_Summary": Quote the **1-3 most relevant paragraphs verbatim** from the source text that contain the core details of the alleged anticompetitive conduct.  Ensure the quoted text directly supports the identified allegation and mentions the specific drug involved.  Do not summarize.  If the relevant information spans more than 3 paragraphs, prioritize the most pertinent ones.
    4.  "Involved_Defendants_CoConspirators": List ONLY the company names (e.g., "Sandoz, Taro") explicitly mentioned in the text associated with THIS SPECIFIC allegation as participating in or directly affected by the conduct.
    5.  "Pin_Cite_Page": The exact PAGE NUMBER (e.g., "61", "123") of this chunk, provided as '{{page_num_or_chunk_id}}'.
    6.  "Pin_Cite_Paragraph": The PARAGRAPH NUMBER from the original document if it's explicitly visible (e.g., "251"). If paragraph numbers are not explicit or cannot be clearly determined from the provided text chunk, state "N/A".

    GUIDELINES:
    - THOROUGHNESS: Scan *only* the provided text from this page/chunk for allegations that name a specific drug.
    - GRANULARITY: Each distinct allegation (e.g., a specific agreement, a specific bid rigging instance, a specific price increase for a drug) should be a SEPARATE JSON object, even if for the same drug.
    - STRICT PRODUCT NAME: DO NOT output any allegation where a specific drug name is not clearly and directly identifiable in the text of the allegation itself. General conspiracy discussions without a drug name should be omitted.
    - PRECISION: Ensure "Involved_Defendants_CoConspirators" and "Specific_Allegation_Summary" are strictly derived from the text supporting THAT particular allegation.
    - JSON FORMAT: The final output MUST be a valid JSON object with the "allegations" key.

    Example (Desired Structure for a single page with multiple allegations for one drug):
    {{
      "allegations": [
        {{
          "Product_Name": "Carbamazepine ER (Tegretol XR)",
          "Allegation_Category": "Market Allocation",
          "Specific_Allegation_Summary": "\"251. In 2009, Sandoz and Taro conspired to divide the market for Carbamazepine ER, which included 'discussing who would target Walmart.'\"",
          "Involved_Defendants_CoConspirators": "Sandoz, Taro, Walmart",
          "Pin_Cite_Page": "{page_num_or_chunk_id}",
          "Pin_Cite_Paragraph": "251"
        }},
        {{
          "Product_Name": "Carbamazepine ER (Tegretol XR)",
          "Allegation_Category": "Price Protection / Market Allocation",
          "Specific_Allegation_Summary": "\"273. In 2014, Sandoz 'declined repeated bid requests from Walmart' to protect Taro’s price increase on Carbamazepine ER.\"",
          "Involved_Defendants_CoConspirators": "Sandoz, Taro, Walmart",
          "Pin_Cite_Page": "{page_num_or_chunk_id}",
          "Pin_Cite_Paragraph": "273"
        }}
      ]
    }}

    Analyze the following page text:
    ---BEGIN PAGE TEXT---
    {text_chunk}
    ---END PAGE TEXT---
    
    Your entire response MUST be a single JSON object structured as {{"allegations": [...]}}.
    """

    max_retries = 2
    generation_config = genai.types.GenerationConfig(
        temperature=0.0,
        response_mime_type="application/json",
        max_output_tokens=8192
    )
    safety_settings = [{"category": c, "threshold": "BLOCK_NONE"}
                       for c in genai.types.HarmCategory if c != genai.types.HarmCategory.HARM_CATEGORY_UNSPECIFIED]

    for attempt in range(max_retries):
        try:
            print(f"  [LLM Call] Processing page/chunk '{page_num_or_chunk_id}' (Attempt {attempt + 1})...")
            response = gemini_model_global.generate_content(
                prompt_content,
                generation_config=generation_config,
                safety_settings=safety_settings
            )

            if response.prompt_feedback and response.prompt_feedback.block_reason:
                reason = response.prompt_feedback.block_reason.name
                error_msg = f"Gemini content generation blocked by safety filters ({reason}) for page '{page_num_or_chunk_id}'."
                print(f"  [LLM Error] {error_msg}")
                return [{"Error": error_msg}]

            content_str = response.text.strip()

            if not content_str:
                error_msg = f"Gemini response text is empty for page '{page_num_or_chunk_id}'."
                if response.candidates and response.candidates[0].finish_reason.name == "SAFETY":
                    safety_ratings_str = ", ".join([f"{r.category.name}: {r.probability.name}" for r in
                                                     response.candidates[0].safety_ratings if
                                                     hasattr(r, 'category') and hasattr(r, 'probability')])
                    error_msg += f" Likely blocked by safety. Ratings: [{safety_ratings_str}]"
                print(f"  [LLM Warning] {error_msg}")
                return [{"Error": error_msg}]

            if content_str.startswith("```json"):
                content_str = content_str.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
            elif content_str.startswith("```"):
                content_str = content_str.split("```\n", 1)[1].rsplit("\n```", 1)[0]

            try:
                parsed_json_obj = json.loads(content_str)
                if isinstance(parsed_json_obj, dict) and "allegations" in parsed_json_obj:
                    if isinstance(parsed_json_obj["allegations"], list):
                        print(
                            f"  [LLM Success] Page/Chunk '{page_num_or_chunk_id}' found {len(parsed_json_obj['allegations'])} allegations.")
                        return parsed_json_obj["allegations"]
                    else:
                        error_msg = f"Gemini 'allegations' key is not a list for page '{page_num_or_chunk_id}': {parsed_json_obj['allegations']}"
                        print(f"  [LLM Error] {error_msg}. Content: {content_str[:200]}")
                        return [{"Error": error_msg, "Content_Snippet": content_str[:200]}]
                else:
                    error_msg = f"Gemini returned unexpected JSON for page '{page_num_or_chunk_id}'. Expected 'allegations' key. Got: {content_str[:500]}"
                    print(f"  [LLM Error] {error_msg}. Content: {content_str[:200]}")
                    return [{"Error": error_msg, "Content_Snippet": content_str[:200]}]
            except json.JSONDecodeError as je:
                error_msg = f"Gemini did not return valid JSON for page '{page_num_or_chunk_id}': {content_str[:500]}. Error: {je}"
                print(f"  [LLM Error] {error_msg}. Content: {content_str[:200]}")
                return [{"Error": error_msg, "Content_Snippet": content_str[:200]}]
        except Exception as e:
            error_msg = f"Error calling Google Gemini for page '{page_num_or_chunk_id}' (Attempt {attempt + 1}): {e}"
            print(f"  [LLM Critical] {error_msg}")
            traceback.print_exc()
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return [{"Error": f"Failed Gemini call for page '{page_num_or_chunk_id}' after {max_retries} attempts: {e}"}]

    return [{"Error": f"Failed Gemini call for page '{page_num_or_chunk_id}' after {max_retries} attempts (exhausted retries)."}]


@app.route('/', methods=['GET', 'POST'])
def upload_file():
    results = []  # Initialize results list
    if request.method == 'POST':
        if 'file' not in request.files:
            flash("No file part selected.")
            return render_template('upload.html', results=results)
        file = request.files['file']
        if file.filename == '':
            flash("No file selected.")
            return render_template('upload.html', results=results)

        original_filename = file.filename
        filepath = os.path.join(UPLOAD_FOLDER, original_filename)
        file.save(filepath)
        all_extracted_data = []

        max_concurrent_llm_calls = 50
        executor = ThreadPoolExecutor(max_workers=max_concurrent_llm_calls)
        futures = []

        try:
            if original_filename.lower().endswith('.pdf'):
                with pdfplumber.open(filepath) as pdf:
                    num_pages_to_process = len(pdf.pages)

                    print(f"\n--- Starting concurrent processing of {num_pages_to_process} PDF pages ---")
                    for i in range(num_pages_to_process):
                        page = pdf.pages[i]
                        page_num_display = i + 1
                        text = page.extract_text() if page.extract_text() else ""

                        if text.strip():
                            futures.append(executor.submit(
                                analyze_text_chunk_with_gemini,
                                text,
                                str(page_num_display),
                                original_filename
                            ))
                            print(f"  [Submitted] Page {page_num_display} for LLM analysis.")
                        else:
                            print(f"  [Skipped] Page {page_num_display} (no text extracted).")
                            all_extracted_data.append({
                                "Product_Name": "N/A", # Corrected key
                                "Allegation_Category": "N/A", # Corrected key
                                "Specific_Allegation_Summary": f"No text extracted from PDF page {page_num_display}.", # Corrected key
                                "Involved_Defendants_CoConspirators": "N/A", # Corrected key
                                "Pin_Cite_Page": f"p. {page_num_display}", # Corrected key
                                "Pin_Cite_Paragraph": "N/A" # Corrected key
                            })

            elif original_filename.lower().endswith('.docx'):
                paras_per_chunk = 20  # Reduced for potentially larger text chunks
                doc = Document(filepath)
                all_paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                total_paragraphs = len(all_paragraphs)

                num_chunks_to_process = (total_paragraphs + paras_per_chunk - 1) // paras_per_chunk if total_paragraphs > 0 else 0

                print(f"\n--- Starting concurrent processing of {num_chunks_to_process} DOCX chunks ---")
                chunk_num_display = 1
                for i in range(0, total_paragraphs, paras_per_chunk):
                    chunk_paras = all_paragraphs[i: i + paras_per_chunk]
                    text_chunk = "\n".join(chunk_paras)

                    if text_chunk.strip():
                        chunk_id_display = f"DOCX_Chunk_{chunk_num_display}"
                        futures.append(executor.submit(
                            analyze_text_chunk_with_gemini,
                            text_chunk,
                            chunk_id_display,
                            original_filename
                        ))
                        print(f"  [Submitted] Chunk {chunk_id_display} for LLM analysis.")
                    else:
                        chunk_id_display = f"DOCX_Chunk_{chunk_num_display}"
                        print(f"  [Skipped] Chunk {chunk_id_display} (no text extracted).")
                        all_extracted_data.append({
                            "Product_Name": "N/A", # Corrected key
                            "Allegation_Category": "N/A", # Corrected key
                            "Specific_Allegation_Summary": f"No text extracted from DOCX chunk {chunk_id_display}.", # Corrected key
                            "Involved_Defendants_CoConspirators": "N/A", # Corrected key
                            "Pin_Cite_Page": chunk_id_display, # Corrected key
                            "Pin_Cite_Paragraph": "N/A" # Corrected key
                        })
                    chunk_num_display += 1

            else:
                flash("Unsupported file type. Please upload a PDF or DOCX file.", "danger")
                if os.path.exists(filepath):
                    os.remove(filepath)
                return render_template('upload.html', results=results)

            # --- Collect Results from Futures ---
            print("\n--- Collecting results from concurrent LLM calls ---")
            total_submitted_tasks = len(futures)
            for i, future in enumerate(as_completed(futures)):
                try:
                    extracted_allegations_list = future.result()
                    for item in extracted_allegations_list:
                        # Ensure keys from LLM output (e.g., "Product_Name") are mapped correctly for template
                        # and that error items are also structured with the correct keys
                        if "Error" in item:
                            error_page_id = "N/A"
                            if "for page '" in item.get("Error", ""):
                                try:
                                    error_page_id = item["Error"].split("for page '", 1)[1].split("'", 1)[0]
                                except IndexError:
                                    pass

                            all_extracted_data.append({
                                "Product_Name": "ERROR",
                                "Allegation_Category": item.get("Error", "Unknown LLM Error"),
                                "Specific_Allegation_Summary": item.get("Content_Snippet", "")[:500] + f" ... (Full Error: {item.get('Error', 'N/A')})",
                                "Involved_Defendants_CoConspirators": "N/A",
                                "Pin_Cite_Page": f"P{error_page_id}", # Consistent key with template
                                "Pin_Cite_Paragraph": "Error processing" # Consistent key with template
                            })
                        else:
                            all_extracted_data.append({
                                "Product_Name": item.get("Product_Name", "N/A"),
                                "Allegation_Category": item.get("Allegation_Category", "N/A"),
                                "Specific_Allegation_Summary": item.get("Specific_Allegation_Summary", "N/A"),
                                "Involved_Defendants_CoConspirators": item.get(
                                    "Involved_Defendants_CoConspirators", "N/A"),
                                "Pin_Cite_Page": item.get("Pin_Cite_Page", "N/A"),
                                "Pin_Cite_Paragraph": item.get("Pin_Cite_Paragraph", "N/A")
                            })
                    print(
                        f"  [Collected] Task {i + 1}/{total_submitted_tasks} complete. Total allegations so far: {len(all_extracted_data)} entries.")
                except Exception as e:
                    print(f"  [Collection Critical Error] Error collecting future result {i + 1}/{total_submitted_tasks}: {e}")
                    traceback.print_exc()
                    all_extracted_data.append({
                        "Product_Name": "ERROR",
                        "Allegation_Category": "Future Result Collection Error",
                        "Specific_Allegation_Summary": str(e)[:500],
                        "Involved_Defendants_CoConspirators": "N/A",
                        "Pin_Cite_Page": "N/A", # Consistent key with template
                        "Pin_Cite_Paragraph": "N/A" # Consistent key with template
                    })
            print(f"--- Finished collecting all {total_submitted_tasks} submitted results. ---")

            if not all_extracted_data:
                flash("No specific allegations identified by the LLM or no processable text found.", "info")
                # Ensure an empty or message-only item is sent if no data
                results = [{"Product_Name": "No Data", "Allegation_Category": "N/A",
                            "Specific_Allegation_Summary": "No specific allegations identified by the LLM or no processable text found.",
                            "Involved_Defendants_CoConspirators": "N/A", "Pin_Cite_Page": "N/A",
                            "Pin_Cite_Paragraph": "N/A"}]
            else:
                results = all_extracted_data

        except Exception as e:
            print(f"\n--- ERROR in upload_file (main processing loop): {e} ---")
            traceback.print_exc()
            flash(f"Processing error: {str(e)}", "danger")
            results = [] # Clear results on major error
        finally:
            if 'executor' in locals() and executor:
                executor.shutdown(wait=True)
                print("ThreadPoolExecutor shut down.")

            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    print(f"Cleaned up uploaded file: {filepath}")
                except Exception as e_remove:
                    print(f"Error cleaning up {filepath}: {e_remove}")
                    flash(f"Note: Could not remove temporary file {original_filename}. Manual cleanup may be needed.",
                          "warning")

    return render_template('upload.html', results=results)

if __name__ == '__main__':
    if not gemini_model_global:
        print(
            "\n--- WARNING: Google Gemini model not initialized. Check API Key/Model Name in .env and ensure Gemini client setup was successful. ---")
    else:
        try:
            # For production deployment, use waitress:
            serve(app, host='0.0.0.0', port=5000, threads=10)  # Adjust threads as needed
            # For development with Flask's built-in server (debug=True, use_reloader=False)
            # app.run(debug=True, use_reloader=False)
        except Exception as e:
            print(f"Error starting the Flask application: {e}")
            traceback.print_exc() # This will print the full error stack to your console