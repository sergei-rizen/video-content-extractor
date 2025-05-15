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
import mimetypes # Added for MIME type guessing

# --- Import missing datetime objects ---
from datetime import datetime, timedelta # Added this line

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
# Use the path you provided: Dropbox/Omni/WW/Entities/DC. Omni Coaching/Meetings/Recordings
# <<< --- **VERIFY/CONFIGURE THIS OUTPUT FOLDER PATH** --- >>>
DROPBOX_OUTPUT_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Descriptions' # Corrected based on logs

# List of video file extensions to look for (case-insensitive comparison will be used)
# <<< --- **VERIFY/CONFIGURE THIS LIST** --- >>>
VIDEO_EXTENSIONS = ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv']

# How often the workflow is scheduled to check (used here only for logging clarity)
POLLING_INTERVAL_DESCRIPTION = "Scheduled workflow run"

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


# --- Main Processing Logic ---

print(f"Starting Dropbox watcher and processor ({POLLING_INTERVAL_DESCRIPTION}).")
print(f"Watching Dropbox folder: {DROPBOX_WATCH_FOLDER_PATH}")
print(f"Uploading results to Dropbox folder: {DROPBOX_OUTPUT_FOLDER_PATH}")


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
    # include_media_info=False, include_has_explicit_content=False, include_mounted_folders=False
    # keep the response smaller unless you need that info.
    result = dbx.files_list_folder(path=DROPBOX_WATCH_FOLDER_PATH, recursive=False)
    entries = result.entries
    print(f"Found {len(entries)} entries in the watch folder.")

    files_to_process_now = []
    # Added time filter based on original Google Drive logic, missing in Dropbox implementation
    time_threshold = datetime.utcnow() - timedelta(days=1)
    print(f"Processing files modified since: {time_threshold.isoformat()}")

    for entry in entries:
        # We only care about files (not folders) and process videos among them
        # Also check modification time to only process recent files
        # Added check that server_modified exists and is a datetime object
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
            # Non-video files or folders are skipped implicitly
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
        local_temp_video_path = os.path.join(LOCAL_OUTPUT_DIR, f"temp_video_{file_entry.id}_{file_name}")

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
                if hasattr(file_entry, 'mime_type') and file_entry.mime_type:
                    mime_type_to_use = file_entry.mime_type
                    print(f"Using MIME type from Dropbox metadata: {mime_type_to_use}")
                else:
                    # Attribute doesn't exist or is falsey, guess from the file extension
                    guessed_mime_type, _ = mimetypes.guess_type(file_name)
                    if guessed_mime_type and 'video/' in guessed_mime_type: # Ensure it looks like a video type
                        mime_type_to_use = guessed_mime_type
                        print(f"Guessed MIME type from extension: {mime_type_to_use}")
                    else:
                        # Final fallback/warning - Let Gemini try to infer or potentially fail
                        print(f"Warning: Could not determine confident video MIME type for {file_name}. Proceeding without explicit MIME type (Gemini might reject).")
                        # mime_type_to_use remains None here, letting genai.upload_file handle it


                # Upload file to Gemini
                # Pass the *path* to the downloaded file, not the bytes
                file_obj = genai.upload_file(
                    path=local_temp_video_path, # <--- Pass the local file path here
                    display_name=file_name,
                    mime_type=mime_type_to_use # Pass the determined MIME type (or None)
                )
                # The file_obj returned here *should* be the correct object, but logs show state as '1'
                print(f"Uploaded file to Gemini: {file_obj.uri}, State: {file_obj.state}") # State here is the raw value

                # Wait for processing to complete
                print("Waiting for Gemini processing...")
                processing_start_time = time.time()
                timeout_seconds = 600 # Wait up to 10 minutes for Gemini processing

                # Access the State enum directly from the file_obj's class
                # This is the fix for the "module 'google.generativeai' has no attribute 'File'" error
                if file_obj: # Ensure file_obj was successfully created
                   try:
                       FileState = type(file_obj).State
                       terminal_states = [FileState.SUCCEEDED, FileState.State.FAILED, FileState.State.CANCELLED]
                   except Exception as e:
                       print(f"Error accessing State enum from file_obj type: {e}. Cannot reliably wait for processing.")
                       # Continue, but the waiting loop condition might fail if State enum is truly inaccessible.
                       # A fallback could be to just wait a fixed amount of time or check if state changes from 1/QUEUED

                # Use the terminal state values for checking the raw state value
                # This loop will now correctly wait until the state is one of the terminal values
                # Using the raw state value comparison as in the previous version's fix
                # based on the observation that file_obj.state was '1' (int) in logs.
                # This is a bit of a fallback if the Enum access is still tricky.
                # Let's assume the raw state values for terminal states are known integers.
                # State.SUCCEEDED is 2, FAILED is 3, CANCELLED is 4 based on genai docs/observation.
                # Let's redefine terminal states using raw integers if File.State is problematic.
                terminal_state_values = [2, 3, 4] # 2: SUCCEEDED, 3: FAILED, 4: CANCELLED

                while file_obj.state not in terminal_state_values:
                    # Added a check to ensure file_obj is valid before trying genai.get_file
                    if not file_obj or not hasattr(file_obj, 'name'):
                         print("Error: Gemini file_obj is invalid during wait loop.")
                         raise RuntimeError("Gemini file object invalid during wait loop.")

                    # Check for timeout *before* sleeping
                    if time.time() - processing_start_time > timeout_seconds:
                         print(f"Gemini processing timed out after {timeout_name} seconds for {file_name}. Current state: {file_obj.state}")
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


                # After the loop, file_obj.state IS one of the terminal_state_values
                # Check the final processing state based on the raw state value
                # Use the FileState enum if it was successfully accessed, otherwise use raw values
                succeeded_state = FileState.SUCCEEDED if 'FileState' in locals() else 2
                failed_state = FileState.FAILED if 'FileState' in locals() else 3
                cancelled_state = FileState.CANCELLED if 'FileState' in locals() else 4


                if file_obj.state == succeeded_state:
                    print(f"Gemini processing succeeded for {file_name}.")
                    # Content generation follows this block
                elif file_obj.state == failed_state:
                    error_message = file_obj.error.message if hasattr(file_obj, 'error') and file_obj.error else 'N/A'
                    print(f"Gemini processing failed for {file_name}. State: {file_obj.state}, Error: {error_message}")
                    # Attempt to delete the file from Gemini if possible
                    try:
                       genai.delete_file(file_obj.name)
                       print(f"Deleted failed Gemini file: {file_obj.name}.")
                    except Exception as delete_e:
                       print(f"Error deleting failed Gemini file {file_obj.name}: {delete_e}")
                    raise RuntimeError(f"Gemini processing failed: {file_obj.state} - {error_message}")
                elif file_obj.state == cancelled_state:
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
                print("Generating content with Gemini...")
                model = genai.GenerativeModel('gemini-2.0-flash')

                prompt = """
                You are an expert educational content creator specializing in transforming video content into structured learning materials. Your task is to analyze this video and create a comprehensive educational resource that captures the essence of this one-on-one learning session.

                Your output should:

                1. Begin with an executive summary (3-5 sentences) of the main educational concepts covered
                2. Create a detailed outline with clear hierarchical headings (H1, H2, H3) using markdown formatting
                3. Under each section, extract and organize:
                   - Core concepts and theories presented
                   - Practical methodologies or techniques demonstrated
                   - Key insights or revelations from the instructor
                   - Notable examples or case studies mentioned
                   - Important definitions or terminology introduced

                4. Include a 'Key Takeaways' section with actionable bullet points
                5. Create a 'Further Learning' section suggesting how these concepts could be explored further

                Format your response using rich markdown including:
                - **Bold text** for emphasizing important concepts
                - *Italic text* for definitions or special terminology
                - > Blockquotes for direct quotes from the video
                - Bullet points (-) and numbered lists for organizing information
                - Tables if appropriate for comparing concepts
                - Code blocks if any technical content is presented

                Make the content educational, detailed, and suitable for someone wanting to thoroughly understand and apply the knowledge shared in this one-on-one learning session.
                """

                # Add error handling for content generation
                try:
                    response = model.generate_content([prompt, file_obj])

                    # Check if response contains text
                    if response and hasattr(response, 'text') and response.text: # Check if response object and text attribute exist and text is not empty
                        base_name = os.path.splitext(file_name)[0] # Get filename without extension
                        output_md_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.md")
                        output_html_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.html")

                        # Save the markdown content locally
                        with open(output_md_local_path, "w", encoding='utf-8') as f: # Use utf-8 encoding
                            f.write(response.text)
                        print(f"Saved markdown locally: {output_md_local_path}")

                        # Convert to HTML and save locally
                        html_content = markdown.markdown(response.text)
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
                        # except: pass


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
