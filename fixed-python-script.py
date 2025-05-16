import os
import json
import time
import dropbox
import google.generativeai as genai
import markdown
import mimetypes

from datetime import datetime, timedelta
from google.generativeai import GenerationConfig

# --- Configuration ---
# Get secrets from environment variables passed by GitHub Actions
DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# The path to the folder in your Dropbox account to WATCH.
# Remember Dropbox paths start with '/'. If using 'App folder',
# paths are relative to the app's root, so '/' is the app folder root.
# <<< --- **VERIFY/CONFIGURE THIS WATCH FOLDER PATH** --- >>>
DROPBOX_WATCH_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Recordings'

# The path to the folder in your Dropbox account where RESULTS should be saved.
# Use the path you provided: Dropbox/Omni/WW/Entities/DC. Omni Coaching/Meetings/Descriptions
# <<< --- **VERIFY/CONFIGURE THIS OUTPUT FOLDER PATH** --- >>>
DROPBOX_OUTPUT_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Descriptions'

# List of video file extensions to look for (case-insensitive comparison will be used)
# <<< --- **VERIFY/CONFIGURE THIS LIST** --- >>>
VIDEO_EXTENSIONS = ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv']

# How often the workflow is scheduled to check (used here only for logging clarity)
POLLING_INTERVAL_DESCRIPTION = "Scheduled workflow run"

# --- Gemini Configuration File Paths ---
# <<< --- **VERIFY THESE PATHS** --- >>>
GEMINI_CONFIG_PATH = 'gemini_config.json'
GEMINI_PROMPT_TEMPLATE_PATH = 'video_description_prompt_template.md'
GEMINI_EXAMPLE_OUTPUT_PATH = 'description_example_output.md'

# Check for required environment variables early
if not DROPBOX_ACCESS_TOKEN:
    print("Error: DROPBOX_ACCESS_TOKEN environment variable is not set.")
    exit(1)
if not GEMINI_API_KEY:
    print("Error: GEMINI_API_KEY environment variable is not set.")
    exit(1)

# --- Helper Functions ---

def read_json_config(config_path):
    """Reads configuration from a JSON file."""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        print(f"Successfully read configuration from '{config_path}'.")
        return config
    except FileNotFoundError:
        print(f"Error: Configuration file not found at '{config_path}'.")
        return None
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from '{config_path}': {e}")
        return None
    except Exception as e:
        print(f"Error reading configuration file '{config_path}': {e}")
        return None

def read_text_file(file_path):
    """Reads content from a text file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        print(f"Successfully read file from '{file_path}'.")
        return content
    except FileNotFoundError:
        print(f"Error: File not found at '{file_path}'.")
        return None
    except Exception as e:
        print(f"Error reading file '{file_path}': {e}")
        return None

def is_video_file(file_name):
    """Checks if a file name ends with a known video extension (case-insensitive)."""
    name, ext = os.path.splitext(file_name)
    return ext.lower() in VIDEO_EXTENSIONS

def download_file_from_dropbox(dbx_client, dropbox_path, local_path):
    """Downloads a file from Dropbox to a local path."""
    print(f"Downloading '{dropbox_path}' from Dropbox to '{local_path}'...")
    try:
        local_dir = os.path.dirname(local_path)
        if local_dir:
           os.makedirs(local_dir, exist_ok=True)

        with open(local_path, "wb") as f:
            metadata, res = dbx_client.files_download(path=dropbox_path)
            f.write(res.content)
        print(f"Successfully downloaded '{dropbox_path}' ({metadata.size} bytes)")
        return True
    except dropbox.exceptions.ApiError as e:
        print(f"Error downloading file {dropbox_path}: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during download of {dropbox_path}: {e}")
        return False

def upload_file_to_dropbox(dbx_client, local_path, dropbox_target_path):
    """Uploads a local file to a specific path in Dropbox."""
    file_name = os.path.basename(local_path)
    print(f"Uploading '{file_name}' from '{local_path}' to Dropbox path '{dropbox_target_path}'...")

    try:
        with open(local_path, 'rb') as f:
            dbx_client.files_upload(
                f.read(),
                dropbox_target_path,
                mode=dropbox.files.WriteMode('overwrite'),
                mute=True
            )
        print(f"Successfully uploaded '{file_name}' to Dropbox.")
        return True
    except dropbox.exceptions.ApiError as e:
        print(f"Error uploading '{file_name}' to Dropbox path '{dropbox_target_path}': {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during upload of '{file_name}' to Dropbox: {e}")
        return False

# --- Main Processing Logic ---

# --- Load Configuration and Prompt Files ---
gemini_config = read_json_config(GEMINI_CONFIG_PATH)
if gemini_config is None:
    exit(1)

prompt_template_content = read_text_file(GEMINI_PROMPT_TEMPLATE_PATH)
if prompt_template_content is None:
    exit(1)

example_output_content = read_text_file(GEMINI_EXAMPLE_OUTPUT_PATH)
if example_output_content is None:
    exit(1)

# Construct the final prompt string by inserting the example content into the template
final_prompt_string = prompt_template_content.replace('[INSERT_EXAMPLE_TEXT_HERE]', example_output_content)
if '[INSERT_EXAMPLE_TEXT_HERE]' in final_prompt_string:
    print("Warning: Placeholder [INSERT_EXAMPLE_TEXT_HERE] not found in template. Prompt might be malformed.")

# Extract Gemini settings from config
GEMINI_MODEL_NAME = gemini_config.get("model_name", "gemini-1.5-pro-latest") # Use the default confirmed working model
raw_gen_config = gemini_config.get("generation_config", {})
PROCESSING_TIMEOUT_SECONDS = gemini_config.get("processing_timeout_seconds", 1800) # Default to 30 min

# Create GenerationConfig object
try:
    GEMINI_GENERATION_CONFIG = GenerationConfig(**raw_gen_config)
    print(f"Using Gemini model: {GEMINI_MODEL_NAME}")
    print(f"Using Generation Config: {GEMINI_GENERATION_CONFIG}")
    print(f"Using Processing Timeout: {PROCESSING_TIMEOUT_SECONDS} seconds")
except Exception as e:
    print(f"Error creating GenerationConfig from config: {e}")
    exit(1)

# Configure the Gemini API (moved down after loading config)
try:
    genai.configure(api_key=GEMINI_API_KEY)
    print("Successfully configured Gemini API.")
    # Diagnostic code to list models previously here, now removed

except Exception as e:
    print(f"Error configuring Gemini API: {e}")
    exit(1)

# Build the Dropbox API client
try:
    dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
    dbx.users_get_current_account()
    print("Successfully connected to Dropbox API.")
except dropbox.exceptions.AuthError:
    print("\nError: Invalid Dropbox access token. Please check your secret.")
    exit(1)
except Exception as e:
     print(f"An unexpected error occurred during initial Dropbox connection: {e}")
     exit(1)

# --- State Management (remains the same) ---
LOCAL_OUTPUT_DIR = 'output'
os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)
PROCESSED_FILES_STATE_FILE = 'processed_files.json'
processed_file_paths = set()

if os.path.exists(PROCESSED_FILES_STATE_FILE):
    try:
        with open(PROCESSED_FILES_STATE_FILE, 'r') as f:
            processed_file_paths = set(json.load(f))
        print(f"Loaded {len(processed_file_paths)} processed file paths from state file.")
    except (json.JSONDecodeError, Exception) as e:
        print(f"Warning: Error loading {PROCESSED_FILES_STATE_FILE}: {e}. Starting with empty processed list.")
        processed_file_paths = set()
else:
    print(f"No {PROCESSED_FILES_STATE_FILE} found. Starting with empty processed list.")

print(f"Starting Dropbox watcher and processor ({POLLING_INTERVAL_DESCRIPTION}).")
print(f"Watching Dropbox folder: {DROPBOX_WATCH_FOLDER_PATH}")
print(f"Uploading results to Dropbox folder: {DROPBOX_OUTPUT_FOLDER_PATH}")

try:
    # Check if watch folder exists and is accessible
    try:
        dbx.files_get_metadata(DROPBOX_WATCH_FOLDER_PATH)
        print(f"Watch folder '{DROPBOX_WATCH_FOLDER_PATH}' exists and is accessible.")
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            print(f"Error: Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}' not found or accessible for the provided token.")
            exit(1)
        elif e.error.is_path() and e.error.get_path().is_insufficient_permissions():
             print(f"Error: Insufficient permissions to access Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}'. Check token scopes.")
             exit(1)
        else: raise
    except Exception as e:
        print(f"An unexpected error occurred when checking Dropbox watch folder {DROPBOX_WATCH_FOLDER_PATH}: {e}")
        exit(1)

    # List files in the Dropbox watch folder
    print(f"Listing files in '{DROPBOX_WATCH_FOLDER_PATH}'...")
    result = dbx.files_list_folder(path=DROPBOX_WATCH_FOLDER_PATH, recursive=False)
    entries = result.entries
    print(f"Found {len(entries)} entries in the watch folder.")

    files_to_process_now = []
    time_threshold = datetime.utcnow() - timedelta(days=1)
    print(f"Processing files modified since (UTC): {time_threshold.isoformat()}")

    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata) and hasattr(entry, 'server_modified') and isinstance(entry.server_modified, datetime) and entry.server_modified.replace(tzinfo=None) > time_threshold:
            if is_video_file(entry.name):
                 if entry.path_display not in processed_file_paths:
                      print(f"Identified new/unprocessed video file: {entry.path_display} (Modified: {entry.server_modified})")
                      files_to_process_now.append(entry)
                 else:
                      print(f"Skipping already processed video file: {entry.path_display} (Already in local list)")
        elif isinstance(entry, dropbox.files.FileMetadata):
             print(f"Skipping old or invalid metadata file: {entry.path_display} (Modified: {getattr(entry, 'server_modified', 'N/A')})")

    print(f"Found {len(files_to_process_now)} video files requiring processing in this run.")

    # Process the identified video files one by one
    for file_entry in files_to_process_now:
        dropbox_watch_file_path = file_entry.path_display
        file_name = file_entry.name
        local_temp_video_path = os.path.join(LOCAL_OUTPUT_DIR, f"temp_video_{file_entry.id.replace('id:', '')}_{file_name}")

        print(f"\n--- Processing {file_name} ---")

        if download_file_from_dropbox(dbx, dropbox_watch_file_path, local_temp_video_path):
            file_obj = None

            try:
                print("Uploading video to Gemini API...")
                mime_type_to_use = None
                guessed_mime_type, _ = mimetypes.guess_type(file_name)
                if guessed_mime_type and 'video/' in guessed_mime_type:
                    mime_type_to_use = guessed_mime_type
                    print(f"Guessed MIME type from extension: {mime_type_to_use}")
                else:
                    print(f"Warning: Could not determine confident video MIME type for {file_name} from extension. Proceeding without explicit MIME type (Gemini might reject).")

                file_obj = genai.upload_file(
                    path=local_temp_video_path,
                    display_name=file_name,
                    mime_type=mime_type_to_use
                )
                print(f"Uploaded file to Gemini: {file_obj.uri}, State: {file_obj.state}")

                print("Waiting for Gemini processing...")
                processing_start_time = time.time()

                # Use raw integer state values for robustness
                succeeded_state_value = 2
                failed_state_value = 3
                cancelled_state_value = 4
                terminal_state_values_numeric = [succeeded_state_value, failed_state_value, cancelled_state_value]

                while file_obj.state not in terminal_state_values_numeric:
                    if not file_obj or not hasattr(file_obj, 'name'):
                         print("Error: Gemini file_obj is invalid during wait loop.")
                         raise RuntimeError("Gemini file object invalid during wait loop.")

                    if time.time() - processing_start_time > PROCESSING_TIMEOUT_SECONDS:
                         print(f"Gemini processing timed out after {PROCESSING_TIMEOUT_SECONDS} seconds for {file_name}. Current state: {file_obj.state}")
                         if file_obj and hasattr(file_obj, 'name'):
                             try: genai.delete_file(file_obj.name)
                             except Exception as delete_e: print(f"Error deleting timed-out Gemini file {file_obj.name}: {delete_e}")
                         raise TimeoutError(f"Gemini processing timed out for {file_name}. Current state: {file_obj.state}")

                    time.sleep(15)

                    try:
                        file_obj = genai.get_file(file_obj.name)
                    except Exception as get_file_e:
                         print(f"Warning: Error getting Gemini file status for {file_obj.name}: {get_file_e}. Retrying status check...")
                         continue

                    print(f"  ... State: {file_obj.state}, Elapsed: {int(time.time() - processing_start_time)}s")

                if file_obj.state == succeeded_state_value:
                    print(f"Gemini processing succeeded for {file_name}.")
                elif file_obj.state == failed_state_value:
                    error_message = file_obj.error.message if hasattr(file_obj, 'error') and file_obj.error else 'N/A'
                    print(f"Gemini processing failed for {file_name}. State: {file_obj.state}, Error: {error_message}")
                    try: genai.delete_file(file_obj.name)
                    except Exception as delete_e: print(f"Error deleting failed Gemini file: {delete_e}")
                    raise RuntimeError(f"Gemini processing failed: {file_obj.state} - {error_message}")
                elif file_obj.state == cancelled_state_value:
                     print(f"Gemini processing was cancelled for {file_name}. State: {file_obj.state}")
                     try: genai.delete_file(file_obj.name)
                     except Exception as delete_e: print(f"Error deleting cancelled Gemini file: {delete_e}")
                     raise RuntimeError(f"Gemini processing was cancelled: {file_obj.state}")

                # --- Generate content with Gemini ---
                print("Generating content with Gemini using template, example, and video...")
                model = genai.GenerativeModel(GEMINI_MODEL_NAME)

                try:
                    response = model.generate_content(
                        [final_prompt_string, file_obj],
                        generation_config=GEMINI_GENERATION_CONFIG
                    )

                    response_text = None
                    content_blocked = False # Flag to track if content was blocked by safety filters

                    if response and hasattr(response, 'candidates') and response.candidates:
                         candidate = response.candidates[0]

                         # --- Check finish reason - often indicates filtering ---
                         # Use string comparison for robustness across library versions
                         if hasattr(candidate, 'finish_reason'):
                              finish_reason_str = str(candidate.finish_reason) # Convert Enum to string
                              print(f"Candidate finish reason: {finish_reason_str}")
                              # Common finish reasons for filtering include SAFETY, RECITATION, OTHER
                              if 'SAFETY' in finish_reason_str or 'RECITATION' in finish_reason_str:
                                   print("Candidate finished due to safety or recitation policy.")
                                   content_blocked = True
                                   # Log safety ratings if available, for detail
                                   if hasattr(candidate, 'safety_ratings') and candidate.safety_ratings:
                                        print("Safety ratings:")
                                        for rating in candidate.safety_ratings:
                                             # Check if probability is AT_LEAST_MEDIUM or higher (usually indicates potential issue)
                                             # Value 4=AT_LEAST_MEDIUM, 5=HARM_BLOCKED (for probability Enum)
                                             prob_value = rating.probability # This is an Enum value
                                             print(f"  - {rating.category}: {prob_value} (Probability Enum Value)")
                                             if prob_value >= 4: # Check against Enum value >= AT_LEAST_MEDIUM
                                                 print("    (Likely blocked due to high probability)")


                         # --- If not blocked by finish reason, check individual ratings more thoroughly ---
                         # This catches cases where finish_reason isn't explicitly safety but ratings are high
                         if not content_blocked and hasattr(candidate, 'safety_ratings') and candidate.safety_ratings:
                              print("Checking individual safety ratings (second pass)...")
                              for rating in candidate.safety_ratings:
                                   prob_value = rating.probability # This is an Enum value
                                   if prob_value >= 4: # Check if probability is AT_LEAST_MEDIUM or higher
                                       print(f"  - {rating.category}: {prob_value} (Probability Enum Value) (Potential block indicated)")
                                       content_blocked = True # Mark as blocked if any rating is AT_LEAST_MEDIUM or higher
                              if content_blocked:
                                  print("Content marked as blocked due to high safety rating probabilities.")


                         # --- If not blocked, attempt to extract text ---
                         if not content_blocked and hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                             try:
                                 response_text = ''.join(p.text for p in candidate.content.parts if hasattr(p, 'text'))
                                 # Check for minimal text length - if text is super short, maybe it's also a form of filtering or failed generation
                                 if response_text and len(response_text.strip()) < 50: # Require at least 50 characters for valid content
                                      print(f"Warning: Generated text is very short ({len(response_text.strip())} chars), possibly incomplete or minimal output.")
                                      # Treat minimal text as blocked for saving/uploading purposes, but don't raise error
                                      content_blocked = True # Treat as blocked so it doesn't save the empty/minimal markdown
                                      response_text = None # Clear the text so it goes to the 'else' block


                             except Exception as text_extract_e:
                                 print(f"Warning: Error extracting text from response parts: {text_extract_e}")
                                 response_text = None # Ensure None if extraction fails


                    # Decision point: Save/Upload only if content was not blocked AND valid text was extracted
                    if response_text and not content_blocked:
                        base_name = os.path.splitext(file_name)[0]
                        output_md_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.md")
                        output_html_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.html")

                        with open(output_md_local_path, "w", encoding='utf-8') as f:
                            f.write(response_text)
                        print(f"Saved markdown locally: {output_md_local_path}")

                        # Ensure markdown conversion doesn't fail on unexpected short strings
                        upload_success_html = True # Assume success unless conversion/upload fails
                        try:
                            html_content = markdown.markdown(response_text)
                            with open(output_html_local_path, "w", encoding='utf-8') as f:
                                f.write(html_content)
                            print(f"Saved HTML locally: {output_html_local_path}")
                        except Exception as md_convert_e:
                             print(f"Error converting markdown to HTML for {file_name}: {md_convert_e}")
                             upload_success_html = False # HTML conversion failed

                        print("Attempting to upload results to Dropbox...")
                        dropbox_md_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.md")
                        dropbox_html_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.html")

                        upload_success_md = upload_file_to_dropbox(dbx, output_md_local_path, dropbox_md_target_path)

                        # Only attempt HTML upload if conversion was successful
                        if upload_success_html:
                             upload_success_html = upload_file_to_dropbox(dbx, output_html_local_path, dropbox_html_target_path)
                        else:
                             # If HTML conversion failed, set upload status to False
                             upload_success_html = False


                        if upload_success_md and upload_success_html:
                            print(f"Successfully uploaded both results for {file_name} to Dropbox.")
                            processed_file_paths.add(dropbox_watch_file_path)
                            print(f"Marked '{dropbox_watch_file_path}' as processed (for this run).")
                        elif upload_success_md:
                             print(f"Successfully uploaded Markdown only for {file_name} (HTML upload failed). NOT marking as fully processed in this run.")
                        else:
                            print(f"Upload failed for one or both output files for {file_name}. NOT marking as fully processed in this run.")


                    else: # Handle case where response_text is None or content was blocked
                        if content_blocked:
                             print(f"Skipping saving/uploading for {file_name} due to content blocking or minimal output.")
                             # Decide if you want to treat a blocked response as 'processed' or retry
                             # processed_file_paths.add(dropbox_watch_file_path) # Uncomment to mark blocked as processed
                             # print(f"Marked '{dropbox_watch_file_path}' as processed (blocked).")
                        else: # This covers cases where response_text is None for other reasons (e.g. extraction error)
                             print(f"Gemini generated empty or invalid text content for {file_name} (not explicitly blocked). No output files generated.")

                        # Clean up Gemini file object if it exists and wasn't deleted by failed processing check
                        if file_obj and hasattr(file_obj, 'name'):
                            try: genai.delete_file(file_obj.name)
                            except Exception as delete_e: print(f"Error deleting Gemini file {file_obj.name} after empty/blocked response: {delete_e}")


                except Exception as content_gen_e:
                    print(f"Error during Gemini content generation process for {file_name}: {content_gen_e}")
                    # Don't mark as processed
                    # Clean up Gemini file object if it exists and wasn't deleted by failed processing check
                    if file_obj and hasattr(file_obj, 'name'):
                         try: genai.delete_file(file_obj.name)
                         except Exception as delete_e: print(f"Error deleting Gemini file {file_obj.name} after generation error: {delete_e}")


            except Exception as gemini_process_e:
                 # This catches errors during Gemini upload or the waiting loop
                 print(f"Error during Gemini upload or waiting for processing for {file_name}: {gemini_process_e}")
                 # Don't mark as processed
                 # Clean up the file uploaded to Gemini if it failed during Gemini processing/wait
                 if file_obj and hasattr(file_obj, 'name'):
                     try:
                         genai.delete_file(file_obj.name)
                         print(f"Deleted Gemini file {file_obj.name} after Gemini processing/wait error.")
                     except Exception as delete_e:
                         print(f"Error deleting Gemini file {file_obj.name} after processing/wait error: {delete_e}")

            finally:
                # --- Cleanup ---
                # Clean up the local temporary video file
                if os.path.exists(local_temp_video_path):
                    try: os.remove(local_temp_video_path)
                    except OSError as e: print(f"Error removing temporary local file {local_temp_video_path}: {e}")

                # Clean up local output files after attempted upload - Check if they exist before trying to remove
                base_name = os.path.splitext(file_name)[0]
                output_md_local_path_potential = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.md")
                output_html_local_path_potential = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.html")
                if os.path.exists(output_md_local_path_potential):
                   try: os.remove(output_md_local_path_potential)
                   except OSError as e: print(f"Error removing local file {output_md_local_path_potential}: {e}")
                if os.path.exists(output_html_local_path_potential):
                   try: os.remove(output_html_local_path_potential)
                   except OSError as e: print(f"Error removing local file {output_html_local_path_potential}: {e}")
                print("Cleaned up local temporary files.")

        else:
            print(f"Skipping processing for {file_name} due to download failure from Dropbox.")
            # Don't mark as processed so it can be retried

    print(f"\nSaving updated processed file list locally ({len(processed_file_paths)} entries)...")
    try:
        with open(PROCESSED_FILES_STATE_FILE, 'w') as f:
            json.dump(list(processed_file_paths), f)
        print(f"Successfully saved processed file list locally ({PROCESSED_FILES_STATE_FILE}). Note: This file is NOT persisted across runs without GitHub Actions Artifacts.")
    except Exception as e:
        print(f"Error saving processed file list locally to {PROCESSED_FILES_STATE_FILE}: {e}")

except dropbox.exceptions.ApiError as e:
     print(f"\nDropbox API Error during initial folder check or listing: {e}")
     if e.error.is_path():
         path_error = e.error.get_path()
         if path_error.is_not_found():
             print(f"Error: Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}' not found or accessible for the provided token during listing.")
             exit(1)
         elif path_error.is_insufficient_permissions():
              print(f"Error: Insufficient permissions to access Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}'. Check token scopes.")
              exit(1)
         else: print(f"Unhandled Dropbox Path Error during listing: {e}")
     elif e.error.is_rate_limit():
          print(f"Dropbox Rate Limit Error during listing. Details: {e}. The job might retry depending on workflow settings.")
          exit(1)
     else:
         print(f"Unhandled Dropbox API Error during listing: {e}")
     exit(1)

except Exception as e:
    print(f"\nAn unexpected error occurred during script execution: {e}")
    exit(1)
