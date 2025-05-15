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
DROPBOX_OUTPUT_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Descriptions'

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
    # for model in genai.list_models(): print(model.name)
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
    for entry in entries:
        # We only care about files (not folders) and process videos among them
        if isinstance(entry, dropbox.files.FileMetadata):
            # Check if it's a video file by extension
            if is_video_file(entry.name):
                 # Check if this file path has been processed before *in this run*
                 # NOTE: Without artifact persistence in YAML, this check will NOT
                 # prevent reprocessing files across separate workflow runs.
                 if entry.path_display not in processed_file_paths:
                      print(f"Identified new/unprocessed video file: {entry.path_display}")
                      files_to_process_now.append(entry)
                 else:
                      # This message will only appear if processed_files.json was manually placed
                      # or if a file was processed *earlier in the same run* (if the script
                      # were capable of processing multiple files per run).
                      print(f"Skipping already processed video file: {entry.path_display}")
            # Non-video files or folders are skipped implicitly

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
        if download_file_from_dropbox(dbx, dropbox_watch_file_path, local_temp_video_path):
            file_obj = None # Initialize Gemini file object variable

            try:
                # --- Gemini Processing Part ---
                print("Uploading video to Gemini API...")
                video_data = None
                try:
                    with open(local_temp_video_path, "rb") as f:
                        video_data = f.read()

                    # --- Determine MIME type robustly ---
                    mime_type_to_use = file_entry.mime_type # Try Dropbox provided MIME type first
                    if not mime_type_to_use:
                        # If Dropbox didn't provide it or it's None, guess from the file extension
                        guessed_mime_type, _ = mimetypes.guess_type(file_name)
                        if guessed_mime_type and 'video/' in guessed_mime_type: # Ensure it looks like a video type
                            mime_type_to_use = guessed_mime_type
                            print(f"Guessed MIME type from extension: {mime_type_to_use}")
                        else:
                            # Fallback if guessing fails or isn't a video type
                            # Using the video file's reported MIME type is best, but guessing is next best
                            # If neither works well, Gemini might reject it anyway.
                            # A generic video fallback could be 'video/mp4' but might be wrong.
                            # Let's print a warning and proceed with whatever we got (could be None) or a generic.
                            # Using None might cause genai.upload_file to guess or fail.
                            print(f"Warning: Could not confidently determine video MIME type for {file_name} from Dropbox metadata or extension. Proceeding with best guess or fallback.")
                            # Could set a default like mime_type_to_use = 'video/mp4' if necessary, but relying on the library is often better.


                    # Upload file to Gemini
                    # Pass mime_type_to_use which might be None if determination failed
                    file_obj = genai.upload_file(
                        video_data,
                        display_name=file_name,
                        mime_type=mime_type_to_use # Use the determined MIME type here (could be None)
                    )
                    print(f"Uploaded file to Gemini: {file_obj.uri}, State: {file_obj.state}")

                    # Wait for processing to complete
                    print("Waiting for Gemini processing...")
                    processing_start_time = time.time()
                    timeout_seconds = 600 # Wait up to 10 minutes for Gemini processing

                    # Refresh file object status until processed
                    while True:
                        file_obj = genai.get_file(file_obj.name) # Get the latest state
                        print(f"  ... State: {file_obj.state}, Elapsed: {int(time.time() - processing_start_time)}s")
                        if file_obj.state.is_terminal(): # SUCCEEDED, FAILED, CANCELLED
                            break
                        if time.time() - processing_start_time > timeout_seconds:
                             print(f"Gemini processing timed out after {timeout_seconds} seconds for {file_name}. Current state: {file_obj.state}")
                             raise TimeoutError(f"Gemini processing timed out for {file_name}.")

                        time.sleep(15) # Wait longer between checks (was 10s, 15s is safer for longer processing)

                    # Check the final processing state
                    if file_obj.state.is_succeeded():
                        print(f"Gemini processing succeeded for {file_name}.")
                    elif file_obj.state.is_failed():
                        print(f"Gemini processing failed for {file_name}. State: {file_obj.state}, Error: {file_obj.error.message if file_obj.error else 'N/A'}")
                        raise RuntimeError(f"Gemini processing failed: {file_obj.state} - {file_obj.error.message if file_obj.error else 'N/A'}")
                    elif file_obj.state.is_cancelled():
                         print(f"Gemini processing was cancelled for {file_name}. State: {file_obj.state}")
                         raise RuntimeError(f"Gemini processing was cancelled: {file_obj.state}")
                    else: # Should not happen with is_terminal() check, but for safety
                         print(f"Gemini processing ended in unexpected state: {file_obj.state} for {file_name}.")
                         raise RuntimeError(f"Gemini processing ended in unexpected state: {file_obj.state}")

                    # --- Generate content with Gemini ---
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
                    - Bullet points and numbered lists for organizing information
                    - Tables if appropriate for comparing concepts
                    - Code blocks if any technical content is presented

                    Make the content educational, detailed, and suitable for someone wanting to thoroughly understand and apply the knowledge shared in this one-on-one learning session.
                    """

                    # Add error handling for content generation
                    try:
                        response = model.generate_content([prompt, file_obj])

                        # Check if response contains text
                        if response.text:
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
                            # Construct the target paths in Dropbox
                            dropbox_md_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.md")
                            dropbox_html_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.html")

                            upload_success_md = upload_file_to_dropbox(dbx, output_md_local_path, dropbox_md_target_path)
                            upload_success_html = upload_file_to_dropbox(dbx, output_html_local_path, dropbox_html_target_path)

                            if upload_success_md and upload_success_html:
                                # --- Update Processed State (within THIS run) ---
                                # Add the Dropbox file path from the WATCH folder to the set
                                # Note: This state is NOT persisted across runs without artifact handling.
                                processed_file_paths.add(dropbox_watch_file_path)
                                print(f"Marked '{dropbox_watch_file_path}' as processed (for this run).")
                            else:
                                print(f"Upload failed for one or both output files for {file_name}. NOT marking as fully processed.")
                                # Decide if you want to exit here or continue processing other videos

                        else:
                            print(f"Gemini generated no text content for {file_name}. No output files generated.")
                            # If Gemini generates no text, maybe it couldn't process the video?
                            # Decide if you still want to mark as processed or retry later.
                            # Currently, it's NOT marked processed, so it will be attempted again.

                    except Exception as content_gen_e:
                        print(f"Error during Gemini content generation, saving, or upload for {file_name}: {content_gen_e}")
                        # Don't mark as processed so it could theoretically be retried
                        # Consider deleting the Gemini file object if content generation fails?
                        # This might help prevent Gemini from keeping failed files.
                        # try: genai.delete_file(file_obj.name); print(f"Deleted Gemini file {file_obj.name} after content generation error.")
                        # except Exception as delete_e: print(f"Error deleting Gemini file {file_obj.name} after content generation error: {delete_e}")


                except Exception as gemini_process_e:
                     print(f"Error uploading video to Gemini or waiting for processing for {file_name}: {gemini_process_e}")
                     # Don't mark as processed so it can be retried.
                     # Clean up the file uploaded to Gemini if possible?
                     if file_obj and hasattr(file_obj, 'name'):
                         try:
                             genai.delete_file(file_obj.name)
                             print(f"Deleted Gemini file {file_obj.name} after processing error.")
                         except Exception as delete_e:
                             print(f"Error deleting Gemini file {file_obj.name} after processing error: {delete_e}")

            finally:
                # Clean up the local temporary video file
                if os.path.exists(local_temp_video_path):
                    try: os.remove(local_temp_video_path)
                    except OSError as e: print(f"Error removing temporary local file {local_temp_video_path}: {e}")
                    print(f"Removed temporary local video file: {local_temp_video_path}")

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
# except dropbox.exceptions.AuthError: # This block is redundant now, handled above
#     print("\nError: Invalid Dropbox access token. Please check your secret.")
#     exit(1) # Exit with error code to fail the GitHub Actions job
except dropbox.exceptions.ApiError as e:
     print(f"\nDropbox API Error during initial folder check or listing: {e}")
     if e.error.is_path():
         path_error = e.error.get_path()
         if path_error.is_not_found():
             print(f"Error: A specified Dropbox path was not found or accessible. Watch folder: '{DROPBOX_WATCH_FOLDER_PATH}'")
             # Exit as this is a critical configuration issue
             exit(1)
         elif path_error.is_insufficient_permissions():
             print(f"Error: Insufficient permissions to access Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}'. Check token scopes.")
             exit(1)
         # Add other path error types if needed
         else:
             print(f"Unhandled Dropbox Path Error: {e}")
     elif e.error.is_rate_limit():
          print(f"Dropbox Rate Limit Error. Details: {e}. The job might retry depending on workflow settings.")
          # Don't necessarily exit 1 on rate limit if GitHub Actions retries the job automatically
          # For simplicity, keeping exit(1) for now, but consider softer handling if needed.
          exit(1)
     else:
         print(f"Unhandled Dropbox API Error: {e}")
     exit(1) # Exit with error code on API error

except Exception as e:
    print(f"\nAn unexpected error occurred during script execution: {e}")
    exit(1) # Exit with error code
