import os
import json
import time
import glob
import dropbox
import google.generativeai as genai
import markdown
import fnmatch

# --- Configuration ---
# Get secrets from environment variables passed by GitHub Actions
DROPBOX_ACCESS_TOKEN = os.environ["DROPBOX_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# The path to the folder in your Dropbox account to WATCH.
# BASED on your request, this should be the SAME as the output folder.
# Remember Dropbox paths start with '/'. If using 'App folder',
# paths are relative to the app's root, so '/' is the app folder root.
# <<< --- **FIXED THIS WATCH FOLDER PATH** --- >>>
DROPBOX_WATCH_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Recordings' # NOW WATCHES THE SPECIFIED FOLDER

# The path to the folder in your Dropbox account where RESULTS should be saved.
# Use the path you provided: Dropbox/Omni/WW/Entities/DC. Omni Coaching/Meetings/Recordings
DROPBOX_OUTPUT_FOLDER_PATH = '/Omni/WW/Entities/DC. Omni Coaching/Meetings/Recordings' # This remains the same

# List of video file extensions to look for (case-insensitive comparison will be used)
VIDEO_EXTENSIONS = ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv'] # <<< --- **CONFIGURE THIS** --- >>>

# How often the workflow is scheduled to check (used here only for logging clarity)
POLLING_INTERVAL_DESCRIPTION = "Scheduled workflow run"

# Configure the Gemini API
genai.configure(api_key=GEMINI_API_KEY)

# Build the Dropbox API client (for watching, downloading, and uploading)
dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)

# --- State Management ---
# Create a folder for temporary results on the local runner
LOCAL_OUTPUT_DIR = 'output'
os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)

# Load list of processed files (using Dropbox paths as identifier)
# This file is persisted between runs using GitHub Actions artifacts (IF enabled)
# Since artifact handling was removed, this will always start empty
PROCESSED_FILES_STATE_FILE = 'processed_files.json'
processed_file_paths = set() # Use a set for efficient lookups

if os.path.exists(PROCESSED_FILES_STATE_FILE):
    try:
        with open(PROCESSED_FILES_STATE_FILE, 'r') as f:
            # Load as list, convert to set
            processed_file_paths = set(json.load(f))
        print(f"Loaded {len(processed_file_paths)} processed file paths from state file.")
    except json.JSONDecodeError:
        print(f"Warning: Could not decode {PROCESSED_FILES_STATE_FILE}. Starting with empty processed list.")
        processed_file_paths = set()
    except Exception as e:
         print(f"Warning: Error loading {PROCESSED_FILES_STATE_FILE}: {e}. Starting with empty processed list.")
         processed_file_paths = set()
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
        # Ensure the directory exists locally
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            metadata, res = dbx_client.files_download(path=dropbox_path)
            f.write(res.content)
        print(f"Successfully downloaded '{dropbox_path}' ({metadata.size} bytes)")
        return True
    except dropbox.exceptions.ApiError as e:
        print(f"Error downloading file {dropbox_path}: {e}")
        # Specific error handling could go here, e.g., not_found
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
        with open(local_path, 'rb') as f:
            dbx_client.files_upload(
                f.read(),
                dropbox_target_path,
                mode=dropbox.files.WriteMode('overwrite'), # Or 'add', 'update'
                mute=True # Don't send users notifications
            )
        print(f"Successfully uploaded '{file_name}' to Dropbox.")
        return True
    except dropbox.exceptions.ApiError as e:
        print(f"Error uploading '{file_name}' to Dropbox path '{dropbox_target_path}': {e}")
        # Specific error handling (e.g., AutoRenameError, PathError) could be added
        return False
    except Exception as e:
        print(f"An unexpected error occurred during upload of '{file_name}' to Dropbox: {e}")
        return False


# --- Main Processing Logic ---

print(f"Starting Dropbox watcher and processor ({POLLING_INTERVAL_DESCRIPTION}).")
print(f"Watching Dropbox folder: {DROPBOX_WATCH_FOLDER_PATH}")
print(f"Uploading results to Dropbox folder: {DROPBOX_OUTPUT_FOLDER_PATH}")


try:
    # Check Dropbox connection and folder existence (optional but good practice)
    dbx.users_get_current_account()
    print("Successfully connected to Dropbox API.")

    # Check if watch folder exists and is accessible
    try:
        dbx.files_get_metadata(DROPBOX_WATCH_FOLDER_PATH)
        print(f"Watch folder '{DROPBOX_WATCH_FOLDER_PATH}' exists and is accessible.")
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            print(f"Error: Dropbox watch folder '{DROPBOX_WATCH_FOLDER_PATH}' not found or accessible.")
            # This is a critical configuration error, exit the job
            exit(1)
        else:
            # Re-raise other API errors like permissions, rate limit, etc.
            raise
    except Exception as e:
        print(f"An unexpected error occurred when checking Dropbox watch folder {DROPBOX_WATCH_FOLDER_PATH}: {e}")
        exit(1) # Exit on other unexpected errors


    # Check if output folder exists and is accessible (or can be created implicitly by upload)
    # Uploading will create the necessary parent folders if they don't exist.
    # We don't need an explicit check here.


    # List files in the Dropbox watch folder
    # Using list_folder without a cursor or time filter means we get the current state
    # The processed_file_paths set handles skipping already processed files (if artifact works).
    print(f"Listing files in '{DROPBOX_WATCH_FOLDER_PATH}'...")
    # recursive=False: Only list top-level items
    result = dbx.files_list_folder(path=DROPBOX_WATCH_FOLDER_PATH, recursive=False)
    entries = result.entries
    print(f"Found {len(entries)} entries in the watch folder.")

    files_to_process_now = []
    for entry in entries:
        # We only care about files and process videos among them
        if isinstance(entry, dropbox.files.FileMetadata):
            # Check if it's a video file by extension
            if is_video_file(entry.name):
                 # Check if this file path has been processed before
                 # NOTE: Without artifact persistence, this check won't prevent reprocessing across runs.
                 if entry.path_display not in processed_file_paths:
                      print(f"Identified new/unprocessed video file: {entry.path_display}")
                      files_to_process_now.append(entry)
                 else:
                      # This message will only appear if processed_files.json *somehow* exists in the runner's workspace
                      # or if a file was processed *earlier in the same run*.
                      print(f"Skipping already processed video file: {entry.path_display}")
            # No explicit else needed here for non-video files - they are simply not added to files_to_process_now

    print(f"Found {len(files_to_process_now)} video files requiring processing in this run.")

    # Process the identified video files
    for file_entry in files_to_process_now:
        dropbox_watch_file_path = file_entry.path_display
        file_name = file_entry.name
        # Create a temporary local path to download the file
        # Use a path inside the runner's workspace, /tmp might have permissions issues or be small
        local_temp_video_path = os.path.join('.', LOCAL_OUTPUT_DIR, f"temp_video_{file_entry.id}_{file_name}") # Use LOCAL_OUTPUT_DIR for temps too

        print(f"\n--- Processing {file_name} ---")

        # Download the file from Dropbox
        # Ensure local output directory exists before trying to download into it
        os.makedirs(LOCAL_OUTPUT_DIR, exist_ok=True)
        if download_file_from_dropbox(dbx, dropbox_watch_file_path, local_temp_video_path):
            try:
                # --- Gemini Processing Part ---
                print("Uploading video to Gemini API...")
                video_data = None
                try:
                    with open(local_temp_video_path, "rb") as f:
                        video_data = f.read()

                    # Upload file to Gemini
                    file_obj = genai.upload_file(
                        video_data,
                        display_name=file_name,
                        mime_type=file_entry.mime_type # Use MIME type from Dropbox metadata
                    )
                    print(f"Uploaded file to Gemini: {file_obj.uri}, State: {file_obj.state}")

                    # Wait for processing
                    print("Waiting for Gemini processing...")
                    processing_start_time = time.time()
                    timeout_seconds = 600 # Wait up to 10 minutes

                    # Refresh file object status until processed
                    while True:
                        file_obj = genai.get_file(file_obj.name)
                        print(f"  ... State: {file_obj.state}, Elapsed: {int(time.time() - processing_start_time)}s")
                        if file_obj.state.is_terminal(): # SUCCEEDED, FAILED, CANCELLED
                            break
                        if time.time() - processing_start_time > timeout_seconds:
                             print(f"Gemini processing timed out after {timeout_seconds} seconds for {file_name}. Current state: {file_obj.state}")
                             raise TimeoutError("Gemini processing timed out.")

                        time.sleep(10) # Wait before checking again

                    if file_obj.state.is_succeeded():
                        print(f"Gemini processing succeeded for {file_name}.")
                    elif file_obj.state.is_failed():
                        print(f"Gemini processing failed for {file_name}. State: {file_obj.state}, Error: {file_obj.error}") # Include error detail
                        raise RuntimeError(f"Gemini processing failed: {file_obj.state} - {file_obj.error}")
                    elif file_obj.state.is_cancelled():
                         print(f"Gemini processing was cancelled for {file_name}.")
                         raise RuntimeError(f"Gemini processing was cancelled: {file_obj.state}")
                    else: # Should not happen with is_terminal() check, but for safety
                         print(f"Gemini processing ended in unexpected state: {file_obj.state} for {file_name}.")
                         raise RuntimeError(f"Gemini processing ended in unexpected state: {file_obj.state}")


                    # Generate content with Gemini
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

                    response = model.generate_content([prompt, file_obj])

                    # Check response and save content locally first
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

                        # Ensure output folder structure exists in Dropbox before uploading (optional, upload_file handles this)
                        # dbx.files_create_folder_v2(os.path.dirname(dropbox_md_target_path), autorename=True) # This would create parents

                        if upload_file_to_dropbox(dbx, output_md_local_path, dropbox_md_target_path):
                            print(f"Successfully uploaded {os.path.basename(output_md_local_path)} to Dropbox.")
                        else:
                            print(f"Failed to upload {os.path.basename(output_md_local_path)} to Dropbox.")
                            # Decide how to handle upload failure - maybe raise exception?

                        if upload_file_to_dropbox(dbx, output_html_local_path, dropbox_html_target_path):
                             print(f"Successfully uploaded {os.path.basename(output_html_local_path)} to Dropbox.")
                        else:
                            print(f"Failed to upload {os.path.basename(output_html_local_path)} to Dropbox.")
                            # Decide how to handle upload failure


                        # --- Update Processed State (within THIS run) ---
                        # Add the Dropbox file path from the WATCH folder to the set
                        # Note: This state is NOT persisted across runs without artifact handling.
                        processed_file_paths.add(dropbox_watch_file_path)
                        print(f"Marked '{dropbox_watch_file_path}' as processed (for this run).")

                    else:
                         print(f"Gemini generated no text content for {file_name}. No output files generated.")
                         # Decide if you still want to mark as processed or retry later

                except Exception as processing_e:
                    print(f"Error during Gemini processing or output saving/upload for {file_name}: {processing_e}")
                    # Don't mark as processed so it could theoretically be retried on the next run (but won't skip if it fails here)

            finally:
                # Clean up the local temporary video file
                if os.path.exists(local_temp_video_path):
                    os.remove(local_temp_video_path)
                    print(f"Removed temporary local video file: {local_temp_video_path}")

                # Clean up local output files after attempted upload (optional, but good practice)
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
            print(f"Skipping processing for {file_name} due to download failure.")
            # Don't mark as processed so it can be retried


    # --- Save Updated Processed State (will be lost without artifact upload) ---
    # This part is still in the script, but its effect is limited to the current run.
    # It's harmless to keep it, might be useful if you re-implement state persistence later.
    print(f"\nSaving updated processed file list ({len(processed_file_paths)} entries)...")
    try:
        # Convert set back to list for JSON serialization
        with open(PROCESSED_FILES_STATE_FILE, 'w') as f:
            json.dump(list(processed_file_paths), f)
        print(f"Successfully saved processed file list locally (will be lost without artifact upload).")
    except Exception as e:
        print(f"Error saving processed file list locally: {e}")


except dropbox.exceptions.AuthError:
    print("\nError: Invalid Dropbox access token. Please check your secret.")
    exit(1) # Exit with error code to fail the GitHub Actions job
except dropbox.exceptions.ApiError as e:
     print(f"\nDropbox API Error: {e}")
     if e.error.is_path():
         path_error = e.error.get_path()
         if path_error.is_not_found():
             print(f"Error: A specified Dropbox path was not found. Watch folder: '{DROPBOX_WATCH_FOLDER_PATH}'")
         elif path_error.is_restricted_content():
             print(f"Error: Restricted content issue with a Dropbox path. Watch folder: '{DROPBOX_WATCH_FOLDER_PATH}'")
         else:
             print(f"Unhandled Dropbox Path Error: {e}")
     elif e.error.is_rate_limit():
          print(f"Dropbox Rate Limit Error. Retrying next run. Details: {e}")
     else:
         print(f"Unhandled Dropbox API Error: {e}")
     exit(1) # Exit with error code on API error

except Exception as e:
    print(f"\nAn unexpected error occurred during Dropbox interaction or initial setup: {e}")
    exit(1) # Exit with error code
