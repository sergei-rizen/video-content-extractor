                # --- Generate content with Gemini ---
                print("Generating content with Gemini using template, example, and video...")
                model = genai.GenerativeModel(GEMINI_MODEL_NAME) # Use model name from config

                try:
                    # Pass the final prompt string and the file_obj
                    response = model.generate_content(
                        [final_prompt_string, file_obj],
                        generation_config=GEMINI_GENERATION_CONFIG # Use config object
                    )

                    response_text = None
                    content_blocked = False # Flag to track if content was blocked by safety filters

                    # Check if response has candidates and the primary one has content parts
                    if response and hasattr(response, 'candidates') and response.candidates:
                         candidate = response.candidates[0]

                         # --- Check safety ratings first ---
                         if hasattr(candidate, 'safety_ratings') and candidate.safety_ratings:
                             # Iterate through ratings to see if any are 'BLOCKED' or 'HARM_BLOCKED'
                             # The safety_ratings structure can vary slightly, this is a common way
                             for rating in candidate.safety_ratings:
                                 # The threshold value (e.g., 4 or 5) usually indicates blocked
                                 # Check the API docs for exact threshold values if needed.
                                 # Let's rely on the presence of a rating and a threshold indicating block
                                 if hasattr(rating, 'probability') and rating.probability >= 4: # Assuming 4+ indicates blocked
                                     print(f"Gemini generation likely blocked for category {rating.category}: {rating.probability}.")
                                     content_blocked = True
                                     break # No need to check other ratings if one blocked it

                             if content_blocked:
                                 print(f"Gemini content generation was blocked by safety policy.")
                                 # Don't proceed with extracting/saving blocked content

                         # --- If not blocked, attempt to extract text ---
                         if not content_blocked and hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts:
                             try:
                                 response_text = ''.join(p.text for p in candidate.content.parts if hasattr(p, 'text'))
                                 # Optional: Add a check for minimal text length if needed
                                 # if len(response_text.strip()) < 50: # e.g., require at least 50 characters
                                 #    print("Warning: Generated text is very short, possibly incomplete.")
                                 #    # Decide how to handle very short responses - maybe still save/upload?

                             except Exception as text_extract_e:
                                 print(f"Warning: Error extracting text from response parts: {text_extract_e}")
                                 response_text = None # Ensure None if extraction fails


                    # --- Decision point: Save/Upload only if content was not blocked and text was extracted ---
                    if response_text and not content_blocked: # Check both conditions
                        base_name = os.path.splitext(file_name)[0]
                        output_md_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.md")
                        output_html_local_path = os.path.join(LOCAL_OUTPUT_DIR, f"{base_name}.html")

                        with open(output_md_local_path, "w", encoding='utf-8') as f:
                            f.write(response_text)
                        print(f"Saved markdown locally: {output_md_local_path}")

                        # Ensure markdown conversion doesn't fail on unexpected short strings
                        try:
                            html_content = markdown.markdown(response_text)
                            with open(output_html_local_path, "w", encoding='utf-8') as f:
                                f.write(html_content)
                            print(f"Saved HTML locally: {output_html_local_path}")
                        except Exception as md_convert_e:
                             print(f"Error converting markdown to HTML for {file_name}: {md_convert_e}")
                             # Decide if you want to exit or just skip HTML upload if conversion fails
                             upload_success_html = False # Assume HTML upload failed

                        print("Attempting to upload results to Dropbox...")
                        dropbox_md_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.md")
                        dropbox_html_target_path = os.path.join(DROPBOX_OUTPUT_FOLDER_PATH, f"{base_name}.html")

                        upload_success_md = upload_file_to_dropbox(dbx, output_md_local_path, dropbox_md_target_path)
                        # Only try to upload HTML if conversion succeeded
                        if 'upload_success_html' not in locals(): # Check if flag wasn't set by an error
                             upload_success_html = upload_file_to_dropbox(dbx, output_html_local_path, dropbox_html_target_path)


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
                             print(f"Skipping saving/uploading for {file_name} due to content blocking.")
                             # Optionally, you could still mark as processed here if you consider a blocked response final
                             # processed_file_paths.add(dropbox_watch_file_path)
                             # print(f"Marked '{dropbox_watch_file_path}' as processed (blocked).")
                        else:
                             print(f"Gemini generated empty or invalid text content for {file_name}. No output files generated.")

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

    # ... (rest of the script: save processed list, outer error handling) ...
