import json
from dotenv import load_dotenv
import os
import io
import shutil # Added for directory deletion
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai
from google.genai import types
from googleapiclient.errors import HttpError
import argparse
import time
extension = "txt"
format = "email" # chat or email

load_dotenv(override=True)

# --- Argument Parsing ---
def parse_arguments():
    parser = argparse.ArgumentParser(description="Generate a response to a customer inquiry using Google Drive resources and GenAI.")
    parser.add_argument("-i", "--inquiry", help="The customer inquiry text (will prompt if not provided).")
    parser.add_argument("-d", "--download", action="store_true", help="Download files from Google Drive (otherwise use local files).")
    parser.add_argument("-m", "--model", choices=['flash', 'flash-lite', 'pro'], default='flash', help="Specify the GenAI model to use ('flash', 'flash-lite', or 'pro'). Defaults to 'flash'.")
    args = parser.parse_args()
    return args

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

def get_drive_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("./keys/token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("./keys/credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("./keys/token.json", "w") as token:
            token.write(creds.to_json())
    return build("drive", "v3", credentials=creds)

def download_google_doc(service, file_id, local_filename_base):
    try:
        # Get file metadata to check MIME type
        file_metadata = service.files().get(fileId=file_id, fields='mimeType, name').execute()
        mime_type = file_metadata.get('mimeType')
        original_name = file_metadata.get('name')
        print(f"  File '{original_name}' has MIME type: {mime_type}")

        request = None
        local_filename = f"{local_filename_base}.{extension}" # Default download extension

        if mime_type == 'application/vnd.google-apps.document':
            print(f"  Exporting Google Doc '{original_name}' as text/plain...")
            request = service.files().export_media(fileId=file_id, mimeType="text/plain")
        elif mime_type == 'text/plain':
            print(f"  Downloading plain text file '{original_name}' directly...")
            request = service.files().get_media(fileId=file_id)
        elif mime_type == 'application/vnd.google-apps.spreadsheet':
            print(f"  Exporting Google Sheet '{original_name}' as text/csv...")
            request = service.files().export_media(fileId=file_id, mimeType="text/csv")
            local_filename = f"{local_filename_base}.csv" # Change extension for CSV
        else:
            print(f"  WARNING: Skipping file '{original_name}' (ID: {file_id}). Unsupported MIME type for conversion: {mime_type}")
            return None

        # Proceed with download if request is valid
        if request:
            fh = io.FileIO(local_filename, "wb")
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                if status:
                    print(f"  Download {int(status.progress() * 100)}%.")
            fh.close()
            return local_filename
        else:
             # Should not happen if logic above is correct, but included for safety
             print(f"  ERROR: Could not create download request for file '{original_name}' (ID: {file_id})")
             return None

    except HttpError as error:
        print(f"  An error occurred downloading file ID {file_id}: {error}")
        return None

def prepare_context_files(download_flag):
    """Handles local file check or Drive download, and uploads files to GenAI API once."""
    print("--- Preparing Context Files ---")
    # --- Load File IDs from Environment Variables ---
    file_ids = {}
    prefix = "GDRIVE_"
    for key, value in os.environ.items():
        if key.startswith(prefix):
            # Convert GDRIVE_FILE_ID_SOME_NAME to "Some Name"
            name_parts = key[len(prefix):].split('_')
            name = ' '.join(part.capitalize() for part in name_parts)
            # Specific adjustments removed
            file_ids[name] = value

    if not file_ids:
        print("ERROR: No Google Drive file IDs found in environment variables (expected format: GDRIVE_SOME_NAME=...).")
        return None, None

    print(f"Loaded {len(file_ids)} file IDs from environment.")

    # --- Define Local File Paths Based on Loaded Names ---
    download_dir = "drive"
    expected_local_files = {
        # Adjust 'links' key if needed based on env var name (e.g., GDRIVE_ -> "Links")
        name: os.path.join(download_dir, f"{name}.csv" if name == "Links" or name == "Supported Drones" else f"{name}.{extension}") # default upload extension
        for name in file_ids
    }

    processed_files_paths = {}

    # --- Check Directory Status ---
    dir_exists = os.path.exists(download_dir)
    dir_is_empty = True
    if dir_exists:
        try:
            if not os.listdir(download_dir):
                dir_is_empty = True
            else:
                dir_is_empty = False
        except OSError as e:
            print(f"Warning: Could not check if directory {download_dir} is empty: {e}. Assuming it might not be.")
            dir_is_empty = False # Play safe

    print(f"Directory '{download_dir}' exists: {dir_exists}, Is empty: {dir_is_empty}")

    # --- Determine if Download is Necessary ---
    # Download if flag is set OR directory doesn't exist OR directory is empty
    should_download = download_flag or not dir_exists or dir_is_empty
    if should_download and not download_flag:
        if not dir_exists:
            print(f"Directory '{download_dir}' not found. Files must be downloaded.")
        elif dir_is_empty:
            print(f"Directory '{download_dir}' is empty. Files must be downloaded.")
        # Force download_flag to true if we determined download is necessary
        # This simplifies the later logic blocks
        download_flag = True
        print("Forcing download due to missing or empty directory.")

    # --- Clean directory ONLY if download flag is explicitly set at the start ---
    # Note: We use the original download_flag from args here, not the potentially modified one
    original_download_flag = args.download # Assuming args is accessible or passed appropriately
                                          # If not accessible, need to refactor to pass original flag
    if original_download_flag: # Check the *original* intent from the command line
        print(f"Download flag is set. Attempting to clean directory: {download_dir}")
        if dir_exists:
            try:
                shutil.rmtree(download_dir)
                print(f"Successfully deleted existing directory: {download_dir}")
                dir_exists = False # Update status
                dir_is_empty = True
            except OSError as e:
                print(f"Warning: Error deleting directory {download_dir}: {e}. Proceeding may result in outdated files.")
        else:
            print(f"Directory {download_dir} did not exist, no deletion needed.")

    # --- Ensure download directory exists before proceeding ---
    if not dir_exists:
        try:
            os.makedirs(download_dir, exist_ok=True)
            print(f"Ensured download directory exists: {download_dir}")
        except OSError as e:
            print(f"ERROR: Error creating directory {download_dir}: {e}. Cannot proceed.")
            return None, None # Cannot proceed if directory creation fails

    # --- Download or Use Local Files based on initial check/flag ---
    if should_download: # Use the calculated flag
        print("--- Downloading Files from Google Drive ---")
        drive_service = get_drive_service()
        if not drive_service:
            print("ERROR: Failed to get Google Drive service. Cannot download files.")
            return None, None # Cannot proceed without Drive service
        for name, file_id in file_ids.items():
            # Use expected_local_files to determine the base path before extension
            local_filename_base = os.path.splitext(expected_local_files[name])[0]
            # local_filename_base = os.path.join(download_dir, name) # Old way
            print(f"Processing '{name}' from Google Drive (ID: {file_id})...")
            downloaded_path = download_google_doc(drive_service, file_id, local_filename_base)
            if downloaded_path:
                processed_files_paths[name] = downloaded_path
                # print(f"Finished processing '{name}'. Saved to {downloaded_path}")
            else:
                print(f"Skipped file '{name}' due to download/conversion issue.")
    else:
        # This block now only runs if original download_flag was False AND dir existed AND dir was not empty
        print("--- Skipping Google Drive Download - Using Existing Local Files ---")
        for name, expected_path in expected_local_files.items():
            if os.path.exists(expected_path):
                print(f"Found local file: {expected_path}")
                processed_files_paths[name] = expected_path
            else:
                print(f"Local file not found: {expected_path}. It will not be used.")

    # --- Proceed with GenAI Client Init and Upload ---
    if not processed_files_paths:
         print("No local or downloaded files found/processed. Cannot proceed.")
         return None, None

    # --- Initialize GenAI Client ---
    print("--- Initializing GenAI Client --- ")
    try:
        client = genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY"),
        )
    except Exception as e:
        print(f"Failed to initialize GenAI client: {e}")
        return None, None

    # --- Upload Files to GenAI API Sequentially ---
    print("--- Uploading Files Sequentially to GenAI API --- ")
    genai_file_parts = []

    def upload_single_file(name, file_path):
        try:
            print(f"✅ Uploading {file_path} ({name}) to GenAI API...")
            # Determine MIME type based on file extension
            if file_path.lower().endswith(f'.{extension}'):
                mime_type = 'text/plain'
            elif file_path.lower().endswith('.csv'):
                mime_type = 'text/csv'
            else:
                # Fallback or raise an error if other types are possible and need specific handling
                mime_type = None # Let the library try to guess, or handle error
                print(f"Warning: Could not determine MIME type for {file_path}, attempting upload without it.")

            genai_file = client.files.upload(file=file_path)
            # print(f"Successfully uploaded {name} ({genai_file.name})")
            return types.Part.from_uri(file_uri=genai_file.uri, mime_type=mime_type)
        except Exception as e:
             print(f"Failed to upload {file_path} ({name}) to GenAI: {e}")
             return None # Return None on failure

    # Upload text files first (exported Google Docs)
    print("Uploading text files...")
    for name, file_path in processed_files_paths.items():
        if file_path.lower().endswith(f'.{extension}'):
            result = upload_single_file(name, file_path)
            if result:
                genai_file_parts.append(result)

    # Upload CSV files next (exported Google Sheets)
    # print("Uploading CSV files...")
    # for name, file_path in processed_files_paths.items():
    #     if file_path.lower().endswith('.csv'):
    #         result = upload_single_file(name, file_path)
    #         if result:
    #             genai_file_parts.append(result)

    if not genai_file_parts:
        print("No files were successfully uploaded to GenAI. Aborting.")
        exit()
        # return client, None # Return client in case user wants to try generation without files?

    print(f"Using Gemini {model_name}")
    return client, genai_file_parts


def generate_response(client, file_parts, inquiry_text, model_name, conversation_history):
    """Generates a response, maintaining conversation history."""
    print("\n📤 Inquiry\n")
    print(inquiry_text + "\n")
    print("\n🔍 Response\n")

    last_usage_metadata = None
    response_text = ""

    if not client:
        print("GenAI client is not available.")
        # Return None for metadata and the unchanged history
        return None, conversation_history

    try:
        # Use the model_name argument passed to the function
        if model_name == 'flash':
            model_str = "models/gemini-2.0-flash-001"
        elif model_name == 'flash-lite':
            model_str = "models/gemini-2.0-flash-lite-001"
        elif model_name == 'pro':
            model_str = "models/gemini-2.5-pro-exp-03-25"
        else:
            # Default to pro if unspecified or invalid
            model_str = "models/gemini-2.5-pro-exp-03-25"
            print(f"Warning: Invalid model '{model_name}' specified. Defaulting to {model_str}")

        # --- Construct the prompt based on history ---
        if not conversation_history: # First turn
            try:
                with open("sys_prompts.json", "r") as f:
                    sys_prompts = json.load(f)
            except FileNotFoundError:
                print("Error: sys_prompts.json not found. Exiting.")
                exit()
            
            if format == "chat":
                initial_user_parts = [types.Part.from_text(text=sys_prompts["system_instruction_chat"])]
            elif format == "email":
                initial_user_parts = [types.Part.from_text(text=sys_prompts["system_instruction_email"])]
            elif format == "voice":
                initial_user_parts = [types.Part.from_text(text=sys_prompts["system_instruction_voice"])]
            else:
                initial_user_parts = [types.Part.from_text(text=sys_prompts["system_instruction_chat"])]

            if file_parts:
                initial_user_parts.extend(file_parts)
            initial_user_parts.append(types.Part.from_text(text="--- End of Provided Documents ---"))
            initial_user_parts.append(types.Part.from_text(text="--- User Inquiry ---"))
            initial_user_parts.append(types.Part.from_text(text=inquiry_text))

            # Add the combined initial prompt as the first user message
            conversation_history.append(types.Content(role="user", parts=initial_user_parts))
            contents = conversation_history # Send the history including the new message

        else: # Subsequent turn
            # Append only the new user inquiry
            conversation_history.append(types.Content(role="user", parts=[types.Part.from_text(text=inquiry_text)]))
            contents = conversation_history # Send the whole history

        # Define generation config
        generate_content_config = types.GenerateContentConfig(
            temperature=0,
            response_mime_type="text/plain",
        )

        stream = client.models.generate_content_stream(
            model=model_str,
            contents=contents, # Send the potentially updated history
            config=generate_content_config,
        )

        for chunk in stream:
            if chunk.text:
                print(chunk.text, end="")
                response_text += chunk.text
            if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                last_usage_metadata = chunk.usage_metadata

        # Append the complete model response to the history
        if response_text:
             conversation_history.append(types.Content(role="model", parts=[types.Part.from_text(text=response_text)]))

    except Exception as e:
        print(f"\nAn error occurred during generation: {e}")
        # Don't append model response if there was an error

    # Return metadata and the updated history
    return last_usage_metadata, conversation_history

if __name__ == "__main__":
    args = parse_arguments()
    model_name = args.model

    # Prepare files and client ONCE
    genai_client, prepared_file_parts = prepare_context_files(args.download)

    if not genai_client:
        print("Exiting due to issue with GenAI client initialization.")
        exit()

    # Initialize conversation history
    conversation_history = []

    # Main inquiry loop
    while True:
        inquiry_to_use = args.inquiry # Get inquiry from args for the *first* iteration
        if inquiry_to_use:
            args.inquiry = None # Clear it so we prompt next time
        else:
            try:
                inquiry_to_use = input("\nEnter prompt (or press 'q' to quit): ")
            except EOFError: # Handle Ctrl+D
                print("\nExiting.")
                break

        if not inquiry_to_use:
            print("Inquiry cannot be empty. Please try again or press 'q' to quit.")
            continue

        if inquiry_to_use.lower() == 'q':
            print("Exiting.")
            break

        # Process this single inquiry, passing and updating history
        start_time = time.time()
        final_usage_metadata, conversation_history = generate_response(
            genai_client,
            prepared_file_parts,
            inquiry_to_use,
            args.model,
            conversation_history # Pass current history
        )
        end_time = time.time()
        duration = end_time - start_time

        # Print summary for this inquiry
        print(f"\n\n=== Summary ===")
        print(f"Time taken: {duration:.2f} seconds")
        if final_usage_metadata:
            print(f"Input Tokens: {final_usage_metadata.prompt_token_count}")
            print(f"Output Tokens: {final_usage_metadata.candidates_token_count}")
            print(f"Total Tokens: {final_usage_metadata.total_token_count}")
        else:
            print("Token usage metadata not available for this inquiry.")
        print("=======================")
