from flask import Flask, request, render_template, jsonify, Response
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
from waitress import serve
from concurrent.futures import ThreadPoolExecutor, as_completed

# Azure Blob Storage imports
import uuid
import io
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "your_very_secret_random_key_here_GEMINI_PRODUCTION_READY")

# --- Azure Blob Storage Configuration ---
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
UPLOAD_CONTAINER_NAME = "uploads"
OUTPUT_CONTAINER_NAME = "outputs"

# Initialize BlobServiceClient globally or lazily
blob_service_client = None
if AZURE_STORAGE_CONNECTION_STRING:
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        print("Azure Blob Storage client initialized.")
        # Ensure containers exist (optional, but good for first run)
        try:
            blob_service_client.create_container(UPLOAD_CONTAINER_NAME)
            print(f"Container '{UPLOAD_CONTAINER_NAME}' created (or already exists).")
        except Exception as e:
            if "ContainerAlreadyExists" not in str(e): # Check for specific error message
                print(f"Warning: Could not create '{UPLOAD_CONTAINER_NAME}' container: {e}")
        try:
            blob_service_client.create_container(OUTPUT_CONTAINER_NAME)
            print(f"Container '{OUTPUT_CONTAINER_NAME}' created (or already exists).")
        except Exception as e:
            if "ContainerAlreadyExists" not in str(e):
                print(f"Warning: Could not create '{OUTPUT_CONTAINER_NAME}' container: {e}")
    except Exception as e:
        print(f"Error initializing Azure Blob Storage client: {e}")
else:
    print("AZURE_STORAGE_CONNECTION_STRING not found. Azure Blob Storage will not work.")

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

# --- NEW: Text Sanitization Function for LLM Input ---
def sanitize_text_for_json(text):
    """
    Sanitizes text to ensure it's safe for inclusion in a JSON string value.
    This replaces problematic characters like unescaped backslashes, double quotes,
    and certain control characters, preparing text before sending to the LLM.
    """
    if not isinstance(text, str):
        return str(text) # Ensure the input is a string

    # Escape backslashes: replace single '\' with double '\\'.
    # This must be done carefully to avoid double-escaping already escaped characters.
    # Simplest reliable way is to escape all backslashes first, then quotes.
    # The LLM is then expected to convert standard newlines (\n) to \\n, etc.
    # If the original text contains 'C:\Users\Docs', it should become 'C:\\Users\\Docs' in JSON.
    text = text.replace('\\', '\\\\')

    # Escape double quotes: replace '"' with '\"'.
    text = text.replace('"', '\\"')
    
    # Replace common control characters (ASCII 0-31, 127) that are problematic in JSON
    # except for standard whitespace that JSON handles ('\t', '\n', '\r').
    # These non-standard control characters are not allowed unescaped in JSON strings.
    # They should ideally be \\uXXXX escaped, but simpler to remove/replace for LLM input robustness.
    # Removing them ensures the LLM doesn't attempt to copy invalid chars verbatim.
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)

    return text


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

    Each allegation object MUST have these 7 keys: # CHANGED: From 6 to 7 keys for Item 4
    1.  "Product_Name": THIS IS MANDATORY. Provide the specific generic drug name(s) (and brand name in parentheses if available, e.g., "Carbamazepine ER (Tegretol XR)") that is the subject of THIS specific allegation. If multiple drugs are involved in the *same single distinct action*, list them comma-separated. If NO specific drug is mentioned in the immediate context of a general anticompetitive discussion, that discussion SHOULD NOT be included in the output. Every row MUST be tied to a specific drug.
    2.  "Allegation_Category": Categorize the primary anticompetitive conduct for THIS allegation (e.g., "Market Allocation", "Price Fixing", "Bid Rigging", "Information Exchange", "Refusal to Compete", "Fair Share Conspiracy", "Other Anticompetitive Conduct"). Choose the most fitting.
    3.  "Specific_Allegation_Summary": Quote the **full, entire relevant paragraphs verbatim** from the source text that contain the core details of the alleged anticompetitive conduct. Ensure the quoted text directly supports the identified allegation and mentions the specific drug involved. Do not summarize. Include all text of *each* paragraph that is truly relevant. Prioritize completeness over brevity for this field. # MODIFIED: For Item 5 (Full Paragraph Text)
    4.  "Involved_Defendants_CoConspirators": List ONLY the company names (e.g., "Sandoz, Taro") explicitly mentioned in the text associated with THIS SPECIFIC allegation as participating in or directly affected by the conduct.
    5.  "Pin_Cite_Page": The exact PAGE NUMBER (e.g., "61", "123") of this chunk, provided as '{{page_num_or_chunk_id}}'.
    6.  "Pin_Cite_Paragraph": The PARAGRAPH NUMBER from the original document if it's explicitly visible (e.g., "251") *within the text you are quoting for "Specific_Allegation_Summary"*. If a paragraph number (e.g., "251.") is visible as a prefix to a quoted paragraph, you MUST extract and provide it here. If paragraph numbers are not explicit or cannot be clearly determined from the provided text chunk, state "N/A". # MODIFIED: For Item 6 (Pin Cites N/A)
    7.  "Other_Named_Entities": List any other relevant individuals or companies (not already listed in "Involved_Defendants_CoConspirators") who are explicitly mentioned in the text of THIS specific allegation and are relevant to it, comma-separated. If none are explicitly mentioned, state "N/A". # ADDED: For Item 4

    GUIDELINES:
    - THOROUGHNESS: Scan *only* the provided text from this page/chunk for allegations that name a specific drug.
    - GRANULARITY: Each distinct allegation (e.g., a specific agreement, a specific bid rigging instance, a specific price increase for a drug) should be a SEPARATE JSON object, even if for the same drug.
    - STRICT PRODUCT NAME: DO NOT output any allegation where a specific drug name is not clearly and directly identifiable in the text of the allegation itself. General conspiracy discussions without a drug name should be omitted.
    - PRECISION: Ensure "Involved_Defendants_CoConspirators" and "Specific_Allegation_Summary" are strictly derived from the text supporting THAT particular allegation.
    - JSON FORMAT: The final output MUST be a valid JSON object with the "allegations" key. All string values within the JSON MUST be properly escaped for JSON syntax. For example, literal newline characters (Python's '\n') must be escaped as '\\n', double quotes (Python's '"') inside a string value must be escaped as '\"', and literal backslashes (Python's '\') must be escaped as '\\\\'. This is crucial for correctly representing verbatim text in JSON.
    
    Example (Desired Structure for a single page with multiple allegations for one drug):
    {{
      "allegations": [
        {{
          "Product_Name": "Carbamazepine ER (Tegretol XR)",
          "Allegation_Category": "Market Allocation",
          "Specific_Allegation_Summary": "\"251. In 2009, Sandoz and Taro conspired to divide the market for Carbamazepine ER, which included 'discussing who would target Walmart.'\"",
          "Involved_Defendants_CoConspirators": "Sandoz, Taro, Walmart",
          "Pin_Cite_Page": "{page_num_or_chunk_id}",
          "Pin_Cite_Paragraph": "251",
          "Other_Named_Entities": "N/A" # ADDED: For Item 4 in example
        }},
        {{
          "Product_Name": "Carbamazepine ER (Tegretol XR)",
          "Allegation_Category": "Price Protection / Market Allocation",
          "Specific_Allegation_Summary": "\"273. In 2014, Sandoz 'declined repeated bid requests from Walmart' to protect Taro’s price increase on Carbamazepine ER.\\\\nThis is a line with a backslash: C:\\\\Users\\\\Doc.\"", # Example with escaped newline and backslash for clarity
          "Involved_Defendants_CoConspirators": "Sandoz, Taro, Walmart",
          "Pin_Cite_Page": "{page_num_or_chunk_id}",
          "Pin_Cite_Paragraph": "273",
          "Other_Named_Entities": "John Doe (CEO)" # ADDED: For Item 4 in example
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

            # Clean markdown JSON block
            if content_str.startswith("```json"):
                content_str = content_str.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
            elif content_str.startswith("```"): # In case it just gives ```
                content_str = content_str.split("```\n", 1)[1].rsplit("\n```", 1)[0]
            
            # Additional cleanup for potential trailing commas or other JSON issues
            content_str = re.sub(r',\s*\]', ']', content_str) # Fix trailing commas in arrays
            content_str = re.sub(r',\s*\}', '}', content_str) # Fix trailing commas in objects

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

@app.route('/', methods=['GET'])
def index():
    """Renders the initial upload form page."""
    return render_template('upload.html', results=[])

@app.route('/analyze', methods=['POST'])
def analyze_document():
    """Handles the file upload and analysis via AJAX, returns JSON results."""
    results = []
    input_blob_name = None # To track the unique input blob name for cleanup
    excel_blob_name = None

    if not blob_service_client:
        return jsonify({"status": "error", "message": "Azure Blob Storage not initialized. Check connection string."}), 500

    try:
        if 'file' not in request.files:
            return jsonify({"status": "error", "message": "No file part selected."}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({"status": "error", "message": "No file selected."}), 400

        original_filename = file.filename
        unique_id = str(uuid.uuid4()) # Generate a unique ID for this request
        input_blob_name = f"{unique_id}_{original_filename}" # Unique name for the uploaded blob

        # Extract complaint name from original filename for Item 3
        complaint_name = os.path.splitext(original_filename)[0] # ADDED: For Item 3

        # Get blob clients for upload and output containers
        upload_container_client = blob_service_client.get_container_client(UPLOAD_CONTAINER_NAME)
        output_container_client = blob_service_client.get_container_client(OUTPUT_CONTAINER_NAME)

        # Upload the file to Azure Blob Storage
        input_blob_client = upload_container_client.get_blob_client(input_blob_name)
        input_blob_client.upload_blob(file.stream, overwrite=True) # Overwrite if same UUID, shouldn't happen often
        print(f"Uploaded '{original_filename}' to blob: '{input_blob_name}' in container '{UPLOAD_CONTAINER_NAME}'")

        # Download the blob content into an in-memory stream for processing by pdfplumber/docx
        download_stream = io.BytesIO()
        input_blob_client.download_blob().readinto(download_stream)
        download_stream.seek(0) # Reset stream position to the beginning for library consumption

        all_extracted_data = []
        max_concurrent_llm_calls = 50
        executor = ThreadPoolExecutor(max_workers=max_concurrent_llm_calls)
        futures = []

        if original_filename.lower().endswith('.pdf'):
            with pdfplumber.open(download_stream) as pdf: # Use the in-memory stream
                num_pages_to_process = len(pdf.pages)
                print(f"\n--- Starting concurrent processing of {num_pages_to_process} PDF pages ---")
                for i in range(num_pages_to_process):
                    page = pdf.pages[i]
                    text = page.extract_text() if page.extract_text() else ""

                    if text.strip():
                        # NEW: Sanitize text before sending to LLM for robustness against JSON errors
                        sanitized_text = sanitize_text_for_json(text)
                        
                        futures.append(executor.submit(
                            analyze_text_chunk_with_gemini,
                            sanitized_text, # CHANGED: Pass sanitized text
                            str(i + 1), # page_num_display
                            original_filename
                        ))
                        print(f"  [Submitted] Page {i + 1} for LLM analysis.")
                    else:
                        print(f"  [Skipped] Page {i + 1} (no text extracted).")
                        all_extracted_data.append({
                            "Product_Name": "N/A",
                            "Allegation_Category": "N/A",
                            "Specific_Allegation_Summary": f"No text extracted from PDF page {i + 1}.",
                            "Involved_Defendants_CoConspirators": "N/A",
                            "Other_Named_Entities": "N/A", # ADDED: For Item 4 (for skipped/error rows)
                            "Pin_Cite_Page": str(i + 1), # Ensure consistent with LLM output for pages
                            "Pin_Cite_Paragraph": "N/A",
                            "Complaint_Name": complaint_name # ADDED: For Item 3 (for skipped/error rows)
                        })

        elif original_filename.lower().endswith('.docx'):
            paras_per_chunk = 20
            doc = Document(download_stream) # Use the in-memory stream
            all_paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            total_paragraphs = len(all_paragraphs)
            num_chunks_to_process = (total_paragraphs + paras_per_chunk - 1) // paras_per_chunk if total_paragraphs > 0 else 0

            print(f"\n--- Starting concurrent processing of {num_chunks_to_process} DOCX chunks ---")
            chunk_num_display = 1
            for i in range(0, total_paragraphs, paras_per_chunk):
                chunk_paras = all_paragraphs[i: i + paras_per_chunk]
                text_chunk = "\n".join(chunk_paras)
                
                chunk_id_display = f"DOCX_Chunk_{chunk_num_display}" # Define chunk_id_display here

                if text_chunk.strip():
                    # NEW: Sanitize text before sending to LLM for robustness against JSON errors
                    sanitized_text_chunk = sanitize_text_for_json(text_chunk)

                    futures.append(executor.submit(
                        analyze_text_chunk_with_gemini,
                        sanitized_text_chunk, # CHANGED: Pass sanitized text chunk
                        chunk_id_display,
                        original_filename
                    ))
                    print(f"  [Submitted] Chunk {chunk_id_display} for LLM analysis.")
                else:
                    print(f"  [Skipped] Chunk {chunk_id_display} (no text extracted).")
                    all_extracted_data.append({
                        "Product_Name": "N/A",
                        "Allegation_Category": "N/A",
                        "Specific_Allegation_Summary": f"No text extracted from DOCX chunk {chunk_id_display}.",
                        "Involved_Defendants_CoConspirators": "N/A",
                        "Other_Named_Entities": "N/A", # ADDED: For Item 4 (for skipped/error rows)
                        "Pin_Cite_Page": chunk_id_display,
                        "Pin_Cite_Paragraph": "N/A",
                        "Complaint_Name": complaint_name # ADDED: For Item 3 (for skipped/error rows)
                    })
                chunk_num_display += 1

        else:
            return jsonify({"status": "error", "message": "Unsupported file type. Please upload a PDF or DOCX file."}), 400

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
                            "Product_Name": "ERROR",
                            "Allegation_Category": item.get("Error", "Unknown LLM Error"),
                            "Specific_Allegation_Summary": item.get("Content_Snippet", "")[:500] + f" ... (Full Error: {item.get('Error', 'N/A')})",
                            "Involved_Defendants_CoConspirators": "N/A",
                            "Other_Named_Entities": "N/A", # ADDED: For Item 4
                            "Pin_Cite_Page": error_page_id,
                            "Pin_Cite_Paragraph": "Error processing",
                            "Complaint_Name": complaint_name # ADDED: For Item 3
                        })
                    else:
                        all_extracted_data.append({
                            "Product_Name": item.get("Product_Name", "N/A"),
                            "Allegation_Category": item.get("Allegation_Category", "N/A"),
                            "Specific_Allegation_Summary": item.get("Specific_Allegation_Summary", "N/A"),
                            "Involved_Defendants_CoConspirators": item.get(
                                "Involved_Defendants_CoConspirators", "N/A"),
                            "Other_Named_Entities": item.get("Other_Named_Entities", "N/A"), # ADDED: For Item 4
                            "Pin_Cite_Page": item.get("Pin_Cite_Page", "N/A"),
                            "Pin_Cite_Paragraph": item.get("Pin_Cite_Paragraph", "N/A"),
                            "Complaint_Name": complaint_name # ADDED: For Item 3
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
                    "Other_Named_Entities": "N/A", # ADDED: For Item 4
                    "Pin_Cite_Page": "N/A",
                    "Pin_Cite_Paragraph": "N/A",
                    "Complaint_Name": complaint_name # ADDED: For Item 3
                })
        print(f"--- Finished collecting all {total_submitted_tasks} submitted results. ---")

        # --- Generate Excel Report ---
        df = pd.DataFrame(all_extracted_data)

        # Post-processing for desired Excel format (sorting and blanking product names)
        if not df.empty and "Product_Name" in df.columns:
            df_processed = df.copy()

            def extract_page_num_for_sort(cite_str):
                try:
                    if isinstance(cite_str, str) and cite_str.isdigit():
                        return int(cite_str)
                    elif isinstance(cite_str, str) and cite_str.startswith('DOCX_Chunk_'):
                        # Extract number for sorting, push DOCX chunks to end if mixed
                        match = re.search(r'DOCX_Chunk_(\d+)', cite_str)
                        if match:
                            return 1000000 + int(match.group(1)) # Large number to put after PDF pages
                except:
                    pass
                return float('inf') # Puts N/A, ERROR, etc. at the end

            df_processed['sortable_pin_cite_page'] = df_processed['Pin_Cite_Page'].apply(extract_page_num_for_sort)
            df_processed.sort_values(by=["Product_Name", 'sortable_pin_cite_page'], inplace=True, kind='mergesort',
                                     na_position='last')
            df_processed.drop('sortable_pin_cite_page', axis=1, inplace=True, errors='ignore')

            # REMOVED: Logic to blank out Product Name Display. For Item 1.
            # df_processed['Product Name Display'] = df_processed['Product_Name']
            # for i in range(1, len(df_processed)):
            #     current_product = df_processed.iloc[i]['Product_Name']
            #     prev_product = df_processed.iloc[i - 1]['Product_Name']
            #     if current_product == prev_product and \
            #             current_product not in ["General Allegation", "ERROR", "N/A",
            #                                      "General Anticompetitive Conduct"]:
            #         df_processed.iloc[i, df_processed.columns.get_loc('Product Name Display')] = ""

            final_columns_ordered = [
                "Product_Name", # CHANGED: Uses original Product_Name for Item 1
                "Allegation_Category",
                "Specific_Allegation_Summary",
                "Complaint_Name", # ADDED: For Item 3
                "Involved_Defendants_CoConspirators",
                "Other_Named_Entities", # ADDED: For Item 4
                "Pin_Cite_Page",
                "Pin_Cite_Paragraph"
            ]

            cols_to_use = [col for col in final_columns_ordered if col in df_processed.columns]
            df_excel = df_processed[cols_to_use].rename(columns={
                "Product_Name": "Product Name", # CHANGED: For Item 1 (renames original Product_Name)
                "Allegation_Category": "Allegation Category",
                "Specific_Allegation_Summary": "Specific Allegation", # RENAMED: For Item 2
                "Complaint_Name": "Complaint Name", # RENAMED: For Item 3
                "Involved_Defendants_CoConspirators": "Involved Defendants/Co-Conspirators (as per the allegation)",
                "Other_Named_Entities": "Other Named Entities (Not Defendants/Co-conspirators)", # RENAMED: For Item 4
                "Pin_Cite_Page": "Pin Cite (Page/Chunk)",
                "Pin_Cite_Paragraph": "Pin Cite (Paragraph #)"
            })
        elif df.empty:
            df_excel = pd.DataFrame(columns=[
                "Product Name", "Allegation Category", "Specific Allegation", # Updated for Item 2
                "Complaint Name", # Added for Item 3
                "Involved Defendants/Co-Conspirators (as per the allegation)",
                "Other Named Entities (Not Defendants/Co-conspirators)", # Added for Item 4
                "Pin Cite (Page/Chunk)", "Pin Cite (Paragraph #)"
            ])
        else: # Fallback if Product_Name column isn't found but df isn't empty
            # This fallback might need manual adjustment if df doesn't have Product_Name but somehow has other data
            # For simplicity, I'm assuming 'Product_Name' will always be there if df is not empty based on LLM output structure.
            df_excel = df.rename(columns={
                "Product_Name": "Product Name", # Updated for Item 1
                "Allegation_Category": "Allegation Category",
                "Specific_Allegation_Summary": "Specific Allegation", # Updated for Item 2
                "Involved_Defendants_CoConspirators": "Involved Defendants/Co-Conspirators (as per the allegation)",
                "Pin_Cite_Page": "Pin Cite (Page/Chunk)",
                "Pin_Cite_Paragraph": "Pin Cite (Paragraph #)"
                # Complaint_Name and Other_Named_Entities would not be present here if Product_Name was missing
            })

        # Save Excel to an in-memory stream and upload to Azure Blob Storage
        file_name_without_extension = os.path.splitext(original_filename)[0]
        excel_blob_name = f"{unique_id}_{file_name_without_extension}-analysis.xlsx" # Unique name for output blob
        
        output_stream = io.BytesIO()
        df_excel.to_excel(output_stream, index=False)
        output_stream.seek(0) # Reset stream for upload

        output_blob_client = output_container_client.get_blob_client(excel_blob_name) 
        
        output_blob_client.upload_blob(output_stream, overwrite=True)
        print(f"\n--- Excel file generated and uploaded to blob: '{excel_blob_name}' in container '{OUTPUT_CONTAINER_NAME}' ---")

        # Prepare results for JSON response
        results_for_json = all_extracted_data
        if not results_for_json:
            results_for_json = [{"Product_Name": "No Data", "Allegation_Category": "N/A",
                                "Specific_Allegation_Summary": "No specific allegations identified by the LLM or no processable text found.",
                                "Involved_Defendants_CoConspirators": "N/A",
                                "Other_Named_Entities": "N/A", # ADDED: For Item 4
                                "Pin_Cite_Page": "N/A",
                                "Pin_Cite_Paragraph": "N/A",
                                "Complaint_Name": "N/A" # ADDED: For Item 3
                                }]

        return jsonify({"status": "success", "results": results_for_json, "excel_filename": excel_blob_name}), 200

    except Exception as e:
        print(f"\n--- ERROR in analyze_document (main processing loop): {e} ---")
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Processing error: {str(e)}"}), 500
    finally:
        if 'executor' in locals() and executor:
            executor.shutdown(wait=True)
            print("ThreadPoolExecutor shut down.")
        
        # We don't remove uploaded files immediately from blob storage
        # as they could be useful for debugging or future reference.
        # Implement a separate blob lifecycle management policy if needed.

@app.route('/download_report/<filename>')
def download_report(filename):
    """Serves the generated Excel report for download from Azure Blob Storage."""
    if not blob_service_client:
        return "Azure Blob Storage not initialized.", 500

    try:
        output_container_client = blob_service_client.get_container_client(OUTPUT_CONTAINER_NAME)
        output_blob_client = output_container_client.get_blob_client(filename)
        
        if not output_blob_client.exists():
            print(f"Blob not found: {filename} in '{OUTPUT_CONTAINER_NAME}' container.")
            return "File not found.", 404
        
        # Download the blob content into an in-memory stream
        blob_data = output_blob_client.download_blob().readall()
        
        # Determine content type (MIME type)
        mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" # For .xlsx
        
        # Extract original filename for download prompt (if it was uniquely named)
        original_download_name = filename
        if '_' in filename: # Assuming format unique_id_originalfilename.xlsx
            try:
                parts = filename.split('_', 1)
                if len(parts) > 1:
                    original_download_name = parts[1] 
            except Exception as e:
                print(f"Could not parse original filename from unique blob name {filename}: {e}")

        response = Response(blob_data, mimetype=mime_type)
        response.headers["Content-Disposition"] = f"attachment; filename={original_download_name}"
        return response
    except Exception as e:
        print(f"Error serving file '{filename}' from blob storage: {e}")
        traceback.print_exc()
        return "Error serving file.", 500

if __name__ == '__main__':
    if not gemini_model_global:
        print(
            "\n--- WARNING: Google Gemini model not initialized. Check API Key/Model Name in .env and ensure Gemini client setup was successful. ---")
    if not blob_service_client:
        print("\n--- WARNING: Azure Blob Storage client not initialized. Check AZURE_STORAGE_CONNECTION_STRING in .env. ---")
    try:
        print("\n--- Starting Flask application using Waitress (for local/Windows dev). For Azure Linux, Gunicorn will be used. ---")
        serve(app, host='0.0.0.0', port=5000, threads=10)
    except Exception as e:
        print(f"Error starting the Flask application: {e}")
        traceback.print_exc()