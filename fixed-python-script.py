--- START OF FILE fixed-python-script-3.py --- # Renamed to reflect changes

import os
import json
import time
# glob is not used, can remove if desired
# import glob
import dropbox
import google.generativeai as genai
import markdown
# fnmatch is not used, can remove if desired
# import fnmatch
import mimetypes

# --- Import missing datetime objects ---
from datetime import datetime, timedelta

# --- Configuration ---
# Get secrets from environment variables passed by GitHub Actions
DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") # Use .get() for safer access

# The path to the folder in your Dropbox account to WATCH.
# BASED on your request, this should be the SAME as the output folder.
# Remember Dropbox paths start with '/'. If using 'App folder',
# paths are relative to the app's root, so '/' is the app folder root.
# <<< --- **VERIFY/CONFIGURE THIS WATCH FOLDER PATH** --- >>>
DROPBOX_WATCH_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Recordings'

# The path to the folder in your Dropbox account where RESULTS should be saved.
# Use the path you provided: Dropbox/Omni/WW/Entities/DC. Omni Coaching/Meetings/Descriptions
# <<< --- **VERIFY/CONFIGURE THIS OUTPUT FOLDER PATH** --- >>>
DROPBOX_OUTPUT_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Descriptions' # Corrected based on logs

# List of video file extensions to look for (case-insensitive comparison will be used)
# <<< --- **VERIFY/CONFIGURE THIS LIST** --- >>>
VIDEO_EXTENSIONS = ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv']

# How often the workflow is scheduled to check (used here only for logging clarity)
POLLING_INTERVAL_DESCRIPTION = "Scheduled workflow run"

# --- Gemini Configuration ---
# File path for the Gemini prompt template
# <<< --- **VERIFY THIS PATH** --- >>>
GEMINI_PROMPT_TEMPLATE_PATH = 'video_description_prompt_template.md' # Assuming it's in the same directory as the script

# Gemini Generation Parameters for Consistency
# Lower temperature reduces randomness, making the output more focused and consistent.
# Lower top_p samples from a smaller set of the most probable tokens, also increasing consistency.
# Adjust slightly within recommended ranges (temp 0.1-0.5, top_p 0.8-0.95) if needed.
GEMINI_GENERATION_CONFIG = genai.GenerationConfig(
    temperature=0.3, # Recommended low temperature for consistency
    top_p=0.9,       # Recommended moderate top_p
    # top_k=1,       # Can optionally set a low top_k as well, but temperature/top_p are often sufficient
    # max_output_tokens=... # Set if you have specific length requirements
)


# Check for required environment variables early
if not DROPBOX_ACCESS_TOKEN:
    print("Error: DROPBOX_ACCESS_TOKEN environment variable is not set.")
    exit(1)
if not GEMINI_API_KEY:
    print("Error: GEMINI_API_KEY environment variable is not set.")
    exit(1)

# Configure the Gemini API
try:
    genai.configure(api_key=GEMINI_API_KEY)
    # Optional: Test Gemini connection/auth if needed, e.g., list models
    # print("Available Gemini models:")
    # for model in genai.list_models():
    #    print(f"- {model.name}")
    # print("-" * 20)
except Exception as e:
    print(f"Error configuring Gemini API: {e}")
    exit(1)


# Build the Dropbox API client (for watching, downloading, and uploading)
try:
    dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
    # Check Dropbox connection immediately
    dbx.users_get_current_account()
    print("Successfully connected to Dropbox API.")
except dropbox.exceptions.AuthError:
    print("\nError: Invalid Dropbox access token. Please check your secret.")
    exit(1) # Exit with error code to fail the GitHub Actions job
except Exception as e:
     print(f"An unexpected error occurred during initial Dropbox connection: {e}")
     exit(1) # Exit with error code


# --- State Management ---
# Create a folder for temporary results on the local runner
LOCAL_OUTPUT_DIR = 'output'
os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)

# Load list of processed files (using Dropbox paths as identifier)
# This file is typically persisted between runs using GitHub Actions artifacts.
# Since artifact handling was explicitly removed from the YAML, this file
# will NOT persist between separate workflow runs. The script will start
# with an empty processed list *for each new run*.
PROCESSED_FILES_STATE_FILE = 'processed_files.json'
processed_file_paths = set() # Use a set for efficient lookups

# Check and load the state file. Will print warnings if it fails.
if os.path.exists(PROCESSED_FILES_STATE_FILE):
    try:
        with open(PROCESSED_FILES_STATE_FILE, 'r') as f:
            # Load as list, convert to set
            processed_file_paths = set(json.load(f))
        print(f"Loaded {len(processed_file_paths)} processed file paths from state file.")
    except json.JSONDecodeError:
        print(f"Warning: Could not decode {PROCESSED_FILES_STATE_FILE}. Starting with empty processed list.")
        processed_file_paths = set() # Reset to empty set on decode error
    except Exception as e:
         print(f"Warning: Error loading {PROCESSED_FILES_STATE_FILE}: {e}. Starting with empty processed list.")
         processed_file_paths = set() # Reset to empty set on other errors
else:
    print(f"No {PROCESSED_FILES_STATE_FILE} found. Starting with empty processed list.")

# --- Helper Functions ---
def is_video_file(file_name):
    """Checks if a file name ends with a known video extension (case-insensitive)."""
    name, ext = os.path.splitext(file_name)
    return ext.lower() in VIDEO_EXTENSIONS

def download_file_from_dropbox(dbx_client, dropbox_path, local_path):
    """Downloads a file from Dropbox to a local path."""
    print(f"Downloading '{dropbox_path}' from Dropbox to '{local_path}'...")
    try:
        # Ensure the local parent directory exists before opening the file
        local_dir = os.path.dirname(local_path)
        if local_dir: # Avoid creating '.' directory if path is just a filename
           os.makedirs(local_dir, exist_ok=True)

        with open(local_path, "wb") as f:
            metadata, res = dbx_client.files_download(path=dropbox_path)
            f.write(res.content)
        print(f"Successfully downloaded '{dropbox_path}' ({metadata.size} bytes)")
        return True
    except dropbox.exceptions.ApiError as e:
        print(f"Error downloading file {dropbox_path}: {e}")
        # Specific error handling could go here, e.g., not_found, insufficient_permissions
        return False
    except Exception as e:
        print(f"An unexpected error occurred during download of {dropbox_path}: {e}")
        return False

def upload_file_to_dropbox(dbx_client, local_path, dropbox_target_path):
    """Uploads a local file to a specific path in Dropbox."""
    file_name = os.path.basename(local_path)
    print(f"Uploading '{file_name}' from '{local_path}' to Dropbox path '{dropbox_target_path}'...")

    try:
        # Use files_upload. Pass mode=overwrite to replace existing files
        # The parent folder structure will be created automatically by files_upload
        with open(local_path, 'rb') as f:
            dbx_client.files_upload(
                f.read(),
                dropbox_target_path,
                mode=dropbox.files.WriteMode('overwrite'), # Use overwrite to replace existing files
                mute=True # Don't send users notifications
            )
        print(f"Successfully uploaded '{file_name}' to Dropbox.")
        return True
    except dropbox.exceptions.ApiError as e:
        print(f"Error uploading '{file_name}' to Dropbox path '{dropbox_target_path}': {e}")
        # Specific error handling (e.g., AutoRenameError, PathError, NoWritePermissionError) could be added
        return False
    except Exception as e:
        print(f"An unexpected error occurred during upload of '{file_name}' to Dropbox: {e}")
        return False

def read_prompt_template(template_path):
    """Reads the Gemini prompt template from a file."""
    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()
        print(f"Successfully read prompt template from '{template_path}'.")
        return template_content
    except FileNotFoundError:
        print(f"Error: Gemini prompt template file not found at '{template_path}'.")
        return None
    except Exception as e:
        print(f"Error reading prompt template file '{template_path}': {e}")
        return None


# --- Main Processing Logic ---

print(f"Starting Dropbox watcher and processor ({POLLING_INTERVAL_DESCRIPTION}).")
print(f"Watching Dropbox folder: {DROPBOX_WATCH_FOLDER_PATH}")
print(f"Uploading results to Dropbox folder: {DROPBOX_OUTPUT_FOLDER_PATH}")

# Read the prompt template once at the start
prompt_template_content = read_prompt_template(GEMINI_PROMPT_TEMPLATE_PATH)
if prompt_template_content is None:
    # If the template couldn't be read, we cannot proceed with Gemini generation
    exit(1)


try:
    # Check if watch folder exists and is accessible before listing
    try:
        dbx.files_get_metadata(DROPBOX_WATCH_FOLDER_PATH)
        print(f"Watch folder '{DROPBOX_WATCH_FOLDER_PATH}' exists and is accessible.")
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            print(f"Error: Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}' not found or accessible for the provided token.")
            # This is a critical configuration error, exit the job
            exit(1)
        elif e.error.is_path() and e.error.get_path().is_insufficient_permissions():
             print(f"Error: Insufficient permissions to access Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}'. Check token scopes.")
             exit(1)
        else:
            # Re-raise other API errors like rate limit, permissions issues other than path, etc.
            raise
    except Exception as e:
        print(f"An unexpected error occurred when checking Dropbox watch folder {DROPBOX_WATCH_FOLDER_PATH}: {e}")
        exit(1) # Exit on other unexpected errors


    # List files in the Dropbox watch folder
    # Using list_folder lists all items in the specified path at the top level
    print(f"Listing files in '{DROPBOX_WATCH_FOLDER_PATH}'...")
    # recursive=False means it only lists items directly in the folder, not subfolders
    result = dbx.files_list_folder(path=DROPBOX_WATCH_FOLDER_PATH, recursive=False)
    entries = result.entries
    print(f"Found {len(entries)} entries in the watch folder.")

    files_to_process_now = []
    # Added time filter based on original Google Drive logic, missing in Dropbox implementation
    # Use UTC time for comparison as Dropbox server_modified is UTC
    time_threshold = datetime.utcnow() - timedelta(days=1) # Process files modified in the last 24 hours
    print(f"Processing files modified since (UTC): {time_threshold.isoformat()}")

    for entry in entries:
        # We only care about files (not folders) and process videos among them
        # Also check modification time to only process recent files
        # Added check that server_modified exists and is a datetime object
        # Ensure datetime comparison is timezone-aware or timezone-naive consistently (using naive here by removing tzinfo from server_modified)
        if isinstance(entry, dropbox.files.FileMetadata) and hasattr(entry, 'server_modified') and isinstance(entry.server_modified, datetime) and entry.server_modified.replace(tzinfo=None) > time_threshold:
            # Check if it's a video file by extension
            if is_video_file(entry.name):
                 # Check if this file path has been processed before *in this run*
                 # NOTE: Without artifact persistence in YAML, this check will NOT
                 # prevent reprocessing files across separate workflow runs.
                 if entry.path_display not in processed_file_paths:
                      print(f"Identified new/unprocessed video file: {entry.path_display} (Modified: {entry.server_modified})")
                      files_to_process_now.append(entry)
                 else:
                      # This message will only appear if processed_files.json was manually placed
                      # or if a file was processed *earlier in the same run*.
                      print(f"Skipping already processed video file: {entry.path_display} (Already in local list)")
            # Non-video files are skipped implicitly by the video extension check
        elif isinstance(entry, dropbox.files.FileMetadata):
             # File is a FileMetadata but didn't pass the time check or server_modified check
             print(f"Skipping old or invalid metadata file: {entry.path_display} (Modified: {getattr(entry, 'server_modified', 'N/A')})")
        # Folders are implicitly skipped by the initial isinstance check


    print(f"Found {len(files_to_process_now)} video files requiring processing in this run.")

    # Process the identified video files one by one
    for file_entry in files_to_process_now:
        dropbox_watch_file_path = file_entry.path_display
        file_name = file_entry.name
        # Create a temporary local path to download the file, inside the local output dir
        # Use file_entry.id in the temp name to help prevent conflicts if names are similar
        # Using a simple counter or timestamp might be safer if file_entry.id isn't guaranteed unique across runs/filesystems
        # Let's stick to file_entry.id for now as it's tied to the file itself.
        local_temp_video_path = os.path.join(LOCAL_OUTPUT_DIR, f"temp_video_{file_entry.id.replace('id:', '')}_{file_name}") # Clean id string


        print(f"\n--- Processing {file_name} ---")

        # Download the file from Dropbox
        # Ensure local output directory exists before trying to download into it
        os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True) # Ensure output directory is created
        # Pass the local_temp_video_path to the download function
        if download_file_from_dropbox(dbx, dropbox_watch_file_path, local_temp_video_path):
            file_obj = None # Initialize Gemini file object variable

            try:
                # --- Gemini Processing Part ---
                print("Uploading video to Gemini API...")
                # --- Determine MIME type robustly ---
                mime_type_to_use = None
                # Safely check if the attribute exists and has a value
                # Note: Dropbox API v2 files.list_folder does *not* return mime_type by default in FileMetadata
                # You'd need dbx.files_get_metadata() for each file to get it reliably.
                # Guessing from extension is more practical here given the listing method.
                guessed_mime_type, _ = mimetypes.guess_type(file_name)
                if guessed_mime_type and 'video/' in guessed_mime_type: # Ensure it looks like a video type
                    mime_type_to_use = guessed_mime_type
                    print(f"Guessed MIME type from extension: {mime_type_to_use}")
                else:
                    # Final fallback/warning - Let Gemini try to infer or potentially fail
                    print(f"Warning: Could not determine confident video MIME type for {file_name} from extension. Proceeding without explicit MIME type (Gemini might reject).")
                    # mime_type_to_use remains None here, letting genai.upload_file handle it


                # Upload file to Gemini
                # Pass the *path* to the downloaded file, not the bytes
                file_obj = genai.upload_file(
                    path=local_temp_video_path, # <--- Pass the local file path here
                    display_name=file_name,
                    mime_type=mime_type_to_use # Pass the determined MIME type (or None)
                )
                print(f"Uploaded file to Gemini: {file_obj.uri}, State: {file_obj.state}") # State here is the raw value


                # Wait for processing to complete
                print("Waiting for Gemini processing...")
                processing_start_time = time.time()
                timeout_seconds = 600 # Wait up to 10 minutes for Gemini processing

                # Access the State enum directly from the file_obj's class
                # This is the correct way to reference the Enum.
                FileState = None # Initialize FileState
                if file_obj: # Ensure file_obj was successfully created
                   try:
                       FileState = type(file_obj).State
                       terminal_states = [FileState.SUCCEEDED, FileState.FAILED, FileState.CANCELLED]
                   except Exception as e:
                       print(f"Error accessing State enum from file_obj type: {e}. Will rely on raw state values for wait condition.")
                       # If enum access fails, fall back to using raw integer state values
                       FileState = None # Ensure FileState is None if access failed


                # Use the FileState enum values if accessible, otherwise use raw integer values
                # Raw state values: 1: QUEUED, 2: SUCCEEDED, 3: FAILED, 4: CANCELLED
                succeeded_state_value = FileState.SUCCEEDED if FileState else 2
                failed_state_value = FileState.FAILED if FileState else 3
                cancelled_state_value = FileState.CANCELLED if FileState else 4
                terminal_state_values_numeric = [succeeded_state_value, failed_state_value, cancelled_state_value]


                while file_obj.state not in terminal_state_values_numeric:
                    # Added a check to ensure file_obj is valid before trying genai.get_file
                    if not file_obj or not hasattr(file_obj, 'name'):
                         print("Error: Gemini file_obj is invalid during wait loop.")
                         raise RuntimeError("Gemini file object invalid during wait loop.")

                    # Check for timeout *before* sleeping
                    if time.time() - processing_start_time > timeout_seconds:
                         print(f"Gemini processing timed out after {timeout_seconds} seconds for {file_name}. Current state: {file_obj.state}")
                         # Attempt to delete the file from Gemini if possible
                         if file_obj and hasattr(file_obj, 'name'):
                             try:
                                 genai.delete_file(file_obj.name)
                                 print(f"Deleted Gemini file {file_obj.name} due to timeout.")
                             except Exception as delete_e:
                                 print(f"Error deleting timed-out Gemini file {file_obj.name}: {delete_e}")
                         raise TimeoutError(f"Gemini processing timed out for {file_name}. Current state: {file_obj.state}")

                    time.sleep(15) # Wait longer between checks (was 10s, 15s is safer for longer processing)

                    # Added safety check for potential rate limits or transient errors getting file status
                    try:
                        file_obj = genai.get_file(file_obj.name) # Get the latest state (updates file_obj for next loop check)
                    except Exception as get_file_e:
                         print(f"Warning: Error getting Gemini file status for {file_obj.name}: {get_file_e}. Retrying status check...")
                         # No need to sleep again here, the loop will sleep next if it continues
                         continue # Try getting status again


                    print(f"  ... State: {file_obj.state}, Elapsed: {int(time.time() - processing_start_time)}s")


                # After the loop, file_obj.state IS one of the terminal state values
                # Check the final processing state based on the raw state value or Enum if accessible
                if file_obj.state == succeeded_state_value:
                    print(f"Gemini processing succeeded for {file_name}.")
                    # Content generation follows this block
                elif file_obj.state == failed_state_value:
                    error_message = file_obj.error.message if hasattr(file_obj, 'error') and file_obj.error else 'N/A'
                    print(f"Gemini processing failed for {file_name}. State: {file_obj.state}, Error: {error_message}")
                    # Attempt to delete the file from Gemini if possible
                    try:
                       genai.delete_file(file_obj.name)
                       print(f"Deleted failed Gemini file: {file_obj.name}.")
                    except Exception as delete_e:
                       print(f"Error deleting failed Gemini file {file_obj.name}: {delete_e}")
                    raise RuntimeError(f"Gemini processing failed: {file_obj.state} - {error_message}")
                elif file_obj.state == cancelled_state_value:
                     print(f"Gemini processing was cancelled for {file_name}. State: {file_obj.state}")
                     # Attempt to delete the file from Gemini if possible
                     try:
                        genai.delete_file(file_obj.name)
                        print(f"Deleted cancelled Gemini file: {file_obj.name}.")
                     except Exception as delete_e:
                        print(f"Error deleting cancelled Gemini file {file_obj.name}: {delete_e}")
                     raise RuntimeError(f"Gemini processing was cancelled: {file_obj.state}")
                # No 'else' needed here, as the while loop condition guarantees one of the terminal states was met.

                # --- Generate content with Gemini ---
                # This step only runs if Gemini processing succeeded (due to the if/elif structure above)
                print("Generating content with Gemini using template and video...")
                # Use the video-capable model
                model = genai.GenerativeModel('gemini-2.0-flash')

                # Pass the prompt template content (as a string) and the file_obj (as a Part)
                # This tells Gemini to analyze the video according to the instructions in the text.
                try:
                    response = model.generate_content(
                        [prompt_template_content, file_obj],
                        generation_config=GEMINI_GENERATION_CONFIG # Apply the configured parameters
                    )

                    # Check if response contains text
                    # Accessing response.text directly might raise an exception if generation fails after processing succeeds (e.g., content filtering)
                    # Safer to check candidate safety ratings or catch the exception
                    response_text = None
                    if response and hasattr(response, 'candidates') and response.candidates:
                        # Check if the primary candidate has text and didn't get blocked
                        candidate = response.candidates[0]
                        if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                             # Try to extract text, handling potential Part types if necessary
                             try:
                                 response_text = ''.join(p.text for p in candidate.content.parts if hasattr(p, 'text'))
                             except Exception as text_extract_e:
                                 print(f"Warning: Error extracting text from response parts: {text_extract_e}")
                                 response_text = None # Ensure response_text is None if extraction fails

                        # Check safety ratings if text wasn't extracted
                        if not response_text and hasattr(candidate, 'safety_ratings') and candidate.safety_ratings:
                            print(f"Gemini generation blocked due to safety policy. Ratings: {candidate.safety_ratings}")
                            # You might want to log more details or handle specific block reasons
                            raise RuntimeError("Gemini content generation blocked by safety policy.")


                    if response_text: # Process the extracted text
                        base_name = os.path.splitext(file_name)[0] # Get filename without extension
                        output_md_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.md")
                        output_html_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.html")

                        # Save the markdown content locally
                        with open(output_md_local_path, "w", encoding='utf-8') as f: # Use utf-8 encoding
                            f.write(response_text)
                        print(f"Saved markdown locally: {output_md_local_path}")

                        # Convert to HTML and save locally
                        # Basic markdown conversion, consider a more robust library if needed
                        html_content = markdown.markdown(response_text)
                        with open(output_html_local_path, "w", encoding='utf-8') as f: # Use utf-8 encoding
                            f.write(html_content)
                        print(f"Saved HTML locally: {output_html_local_path}")

                        # --- Upload results to Dropbox ---
                        print("Attempting to upload results to Dropbox...")
                        # Construct the target paths in Dropbox - using the Output folder
                        dropbox_md_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.md")
                        dropbox_html_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.html")

                        upload_success_md = upload_file_to_dropbox(dbx, output_md_local_path, dropbox_md_target_path)
                        upload_success_html = upload_file_to_dropbox(dbx, output_html_local_path, dropbox_html_target_path)

                        if upload_success_md and upload_success_html:
                            print(f"Successfully uploaded both results for {file_name} to Dropbox.")
                            # --- Update Processed State (within THIS run) ---
                            # Add the Dropbox file path from the WATCH folder as the identifier
                            # Note: This state is NOT persisted across runs without artifact handling.
                            processed_file_paths.add(dropbox_watch_file_path)
                            print(f"Marked '{dropbox_watch_file_path}' as processed (for this run).")
                        else:
                            print(f"Upload failed for one or both output files for {file_name}. NOT marking as fully processed in this run.")
                            # The file will be attempted again on the next run (due to no state persistence)

                    else:
                        print(f"Gemini generated empty or invalid text content for {file_name}. No output files generated.")
                        # If Gemini generates no text, maybe it couldn't process the video?
                        # It's not marked processed, so it will be attempted again on the next run.
                        # Consider deleting the Gemini file object if it didn't produce text?
                        # try: genai.delete_file(file_obj.name); print(f"Deleted Gemini file {file_obj.name} after empty response.")
                        # except: pass # Ignore errors during cleanup


                except Exception as content_gen_e:
                    print(f"Error during Gemini content generation, saving, or upload for {file_name}: {content_gen_e}")
                    # Don't mark as processed
                    # Consider deleting the Gemini file object if content generation fails?
                    # try: genai.delete_file(file_obj.name); print(f"Deleted Gemini file {file_obj.name} after generation error.")
                    # except: pass # Ignore errors during cleanup


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
                    try:
                        os.remove(local_temp_video_path)
                        print(f"Removed temporary local video file: {local_temp_video_path}")
                    except OSError as e: print(f"Error removing temporary local file {local_temp_video_path}: {e}")

                # Clean up local output files after attempted upload
                base_name = os.path.splitext(file_name)[0]
                output_md_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.md")
                output_html_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.html")
                if os.path.exists(output_md_local_path):
                   try: os.remove(output_md_local_path)
                   except OSError as e: print(f"Error removing local file {output_md_local_path}: {e}")
                if os.path.exists(output_html_local_path):
                   try: os.remove(output_html_local_path)
                   except OSError as e: print(f"Error removing local file {output_html_local_path}: {e}")
                print("Cleaned up local temporary files.")

        else:
            print(f"Skipping processing for {file_name} due to download failure from Dropbox.")
            # Don't mark as processed so it can be retried


    # --- Save Updated Processed State (will be lost without artifact upload) ---
    # This part is still in the script, but its effect is limited to the current run.
    # It's harmless to keep it, might be useful if you re-implement state persistence later.
    print(f"\nSaving updated processed file list locally ({len(processed_file_paths)} entries)...")
    try:
        # Convert set back to list for JSON serialization
        with open(PROCESSED_FILES_STATE_FILE, 'w') as f:
            json.dump(list(processed_file_paths), f)
        # Added clarification to the print statement
        print(f"Successfully saved processed file list locally ({PROCESSED_FILES_STATE_FILE}). Note: This file is NOT persisted across runs without GitHub Actions Artifacts.")
    except Exception as e:
        print(f"Error saving processed file list locally to {PROCESSED_FILES_STATE_FILE}: {e}")


# --- Main Error Handling (outside the processing loop) ---
# These catch errors during initial Dropbox connection or the main file listing.
# Specific processing errors per file are caught inside the loop.
# AuthError is caught earlier during connection attempt.
except dropbox.exceptions.ApiError as e:
     print(f"\nDropbox API Error during initial folder check or listing: {e}")
     # More specific error handling already inside the initial folder check.
     # This outer block catches API errors from dbx.files_list_folder
     if e.error.is_path():
         path_error = e.error.get_path()
         if path_error.is_not_found():
             print(f"Error: Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}' not found or accessible for the provided token during listing.")
             # Exit as this is a critical configuration issue
             exit(1)
         elif path_error.is_insufficient_permissions():
              print(f"Error: Insufficient permissions to list Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}'. Check token scopes.")
              exit(1)
         # Add other path error types if needed
         else:
             print(f"Unhandled Dropbox Path Error during listing: {e}")
     elif e.error.is_rate_limit():
          print(f"Dropbox Rate Limit Error during listing. Details: {e}. The job might retry depending on workflow settings.")
          # Don't necessarily exit 1 on rate limit if GitHub Actions retries the job automatically
          # For simplicity, keeping exit(1) for now, but consider softer handling if needed.
          exit(1)
     else:
         print(f"Unhandled Dropbox API Error during listing: {e}")
     exit(1) # Exit with error code on API error

except Exception as e:
    # This catches any other unexpected errors that weren't handled in the specific blocks
    print(f"\nAn unexpected error occurred during script execution: {e}")
    exit(1) # Exit with error code

--- END OF FILE fixed-python-script-3.py ---
