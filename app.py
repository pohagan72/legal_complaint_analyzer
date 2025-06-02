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
    if request.method == 'POST':
        if 'file' not in request.files:
            flash("No file part selected.")
            return render_template('upload.html')
        file = request.files['file']
        if file.filename == '':
            flash("No file selected.")
            return render_template('upload.html')

        page_limit = None
        page_limit_str = request.form.get('page_limit', '')
        if page_limit_str.isdigit():
            limit_val = int(page_limit_str)
            if limit_val > 0:
                page_limit = limit_val
            else:
                flash("Page/Chunk limit must be positive. Processing all pages/chunks.")
        elif page_limit_str:
            flash("Invalid page/chunk limit. Processing all pages/chunks.")

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
                    if page_limit is not None and page_limit < num_pages_to_process:
                        num_pages_to_process = page_limit
                        flash(f"Processing only the first {page_limit} PDF pages as requested.")

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
                                "Product Name": "N/A", "Allegation Category": "N/A",
                                "Specific Allegation Summary": f"No text extracted from PDF page {page_num_display}.",
                                "Involved Defendants/Co-Conspirators (as per the allegation)": "N/A",
                                "Source Complaint": "Walmart Complaint, 2:25-cv-01383, Doc. 1",  # Fixed string
                                "Pin Cite (Page #, Paragraph #)": f"p. {page_num_display}, N/A"
                            })

            elif original_filename.lower().endswith('.docx'):
                paras_per_chunk = 20  # Reduced for potentially larger text chunks
                doc = Document(filepath)
                all_paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                total_paragraphs = len(all_paragraphs)

                num_chunks_to_process = (total_paragraphs + paras_per_chunk - 1) // paras_per_chunk if total_paragraphs > 0 else 0
                if page_limit is not None and page_limit < num_chunks_to_process:
                    num_chunks_to_process = page_limit
                    flash(f"Processing only the first {page_limit} DOCX chunks as requested.")

                print(f"\n--- Starting concurrent processing of {num_chunks_to_process} DOCX chunks ---")
                chunk_num_display = 1
                for i in range(0, total_paragraphs, paras_per_chunk):
                    if chunk_num_display > num_chunks_to_process:
                        break

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
                            "Product Name": "N/A", "Allegation Category": "N/A",
                            "Specific Allegation Summary": f"No text extracted from DOCX chunk {chunk_id_display}.",
                            "Involved Defendants/Co-Conspirators (as per the allegation)": "N/A",
                            "Source Complaint": "Walmart Complaint, 2:25-cv-01383, Doc. 1",
                            "Pin Cite (Page #, Paragraph #)": f"{chunk_id_display}, N/A"
                        })
                    chunk_num_display += 1

            else:
                flash("Unsupported file type. Please upload a PDF or DOCX file.", "danger")
                if os.path.exists(filepath):
                    os.remove(filepath)
                return render_template('upload.html')

            # --- Collect Results from Futures ---
            print("\n--- Collecting results from concurrent LLM calls ---")
            total_submitted_tasks = len(futures)
            for i, future in enumerate(as_completed(futures)):
                try:
                    extracted_allegations_list = future.result()
                    for item in extracted_allegations_list:
                        if "Error" in item:
                            error_page_id = "N/A"
                            if "for page '" in item.get("Error", ""):
                                try:
                                    error_page_id = item["Error"].split("for page '", 1)[1].split("'", 1)[0]
                                except IndexError:
                                    pass

                            all_extracted_data.append({
                                "Product Name": "ERROR",
                                "Allegation Category": item.get("Error", "Unknown LLM Error"),
                                "Specific Allegation Summary": item.get("Content_Snippet", "")[:500] + f" ... (Full Error: {item.get('Error', 'N/A')})",  # Append full error message
                                "Involved Defendants/Co-Conspirators (as per the allegation)": "N/A",
                                "Source Complaint": "Walmart Complaint, 2:25-cv-01383, Doc. 1",
                                "Pin Cite (Page #, Paragraph #)": f"P{error_page_id}, Error processing"
                            })
                        else:
                            all_extracted_data.append({
                                "Product Name": item.get("Product_Name", "N/A"),
                                "Allegation Category": item.get("Allegation_Category", "N/A"),
                                "Specific Allegation Summary": item.get("Specific_Allegation_Summary", "N/A"),
                                "Involved Defendants/Co-Conspirators (as per the allegation)": item.get(
                                    "Involved_Defendants_CoConspirators", "N/A"),
                                "Source Complaint": "Walmart Complaint, 2:25-cv-01383, Doc. 1",
                                "Pin Cite (Page #, Paragraph #)": f"p. {item.get('Pin_Cite_Page', 'N/A')}, ¶{item.get('Pin_Cite_Paragraph', 'N/A')}"
                            })
                    print(
                        f"  [Collected] Task {i + 1}/{total_submitted_tasks} complete. Total allegations so far: {len(all_extracted_data)} entries.")
                except Exception as e:
                    print(f"  [Collection Critical Error] Error collecting future result {i + 1}/{total_submitted_tasks}: {e}")
                    traceback.print_exc()
                    all_extracted_data.append({
                        "Product Name": "ERROR",
                        "Allegation Category": "Future Result Collection Error",
                        "Specific Allegation Summary": str(e)[:500],
                        "Involved Defendants/Co-Conspirators (as per the allegation)": "N/A",
                        "Source Complaint": "Walmart Complaint, 2:25-cv-01383, Doc. 1",
                        "Pin Cite (Page #, Paragraph #)": "N/A"
                    })
            print(f"--- Finished collecting all {total_submitted_tasks} submitted results. ---")

            if not all_extracted_data:
                flash("No specific allegations identified by the LLM or no processable text found.", "info")
                all_extracted_data.append({"Message": "No specific allegations identified by the LLM."})

            df = pd.DataFrame(all_extracted_data)

            # Post-processing for desired Excel format (sorting and blanking product names)
            if not df.empty and "Product Name" in df.columns:
                df_processed = df.copy()

                def extract_page_num_for_sort(cite_str):
                    try:
                        if isinstance(cite_str, str) and cite_str.startswith("p. "):
                            match = re.search(r"p\. (\d+),", cite_str)
                            if match:
                                return int(match.group(1))
                    except:
                        pass
                    return float('inf')

                df_processed['sortable_page'] = df_processed['Pin Cite (Page #, Paragraph #)'].apply(
                    extract_page_num_for_sort)

                df_processed.sort_values(by=["Product Name", 'sortable_page'], inplace=True, kind='mergesort',
                                         na_position='last')
                df_processed.drop('sortable_page', axis=1, inplace=True, errors='ignore')

                df_processed['Product Name Display'] = df_processed['Product Name']
                for i in range(1, len(df_processed)):
                    current_product = df_processed.iloc[i]['Product Name']
                    prev_product = df_processed.iloc[i - 1]['Product Name']
                    if current_product == prev_product and \
                            current_product not in ["General Allegation", "ERROR", "N/A",
                                                     "General Anticompetitive Conduct"]:
                        df_processed.iloc[i, df_processed.columns.get_loc('Product Name Display')] = ""

                final_columns_ordered = ["Product Name Display", "Allegation Category", "Specific Allegation Summary",
                                        "Involved Defendants/Co-Conspirators (as per the allegation)",
                                        "Source Complaint", "Pin Cite (Page #, Paragraph #)"]

                cols_to_use = [col for col in final_columns_ordered if col in df_processed.columns]
                df_excel = df_processed[cols_to_use].rename(columns={"Product Name Display": "Product Name"})
            elif df.empty:
                df_excel = pd.DataFrame(columns=["Product Name", "Allegation Category", "Specific Allegation Summary",
                                                 "Involved Defendants/Co-Conspirators (as per the allegation)",
                                                 "Source Complaint", "Pin Cite (Page #, Paragraph #)"])
            else:
                df_excel = df

            # Construct the Excel filename
            file_name_without_extension = os.path.splitext(original_filename)[0]
            excel_filename = f"{file_name_without_extension}-analysis.xlsx"
            excel_filepath = os.path.join(OUTPUT_FOLDER, excel_filename)
            df_excel.to_excel(excel_filepath, index=False)
            print(f"\n--- Excel file generated: {excel_filepath} ---")
            return send_file(excel_filepath, as_attachment=True)

        except Exception as e:
            print(f"\n--- ERROR in upload_file (main processing loop): {e} ---")
            traceback.print_exc()
            flash(f"Processing error: {str(e)}", "danger")
            return render_template('upload.html')
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

    return render_template('upload.html')


if __name__ == '__main__':
    if not gemini_model_global:
        print(
            "\n--- WARNING: Google Gemini model not initialized. Check API Key/Model Name in .env and ensure Gemini client setup was successful. ---")
    else:
        # For production deployment, use waitress:
        serve(app, host='0.0.0.0', port=5000, threads=10)  # Adjust threads as needed
        # For development with Flask's built-in server (debug=True, use_reloader=False)
        # app.run(debug=True, use_reloader=False)