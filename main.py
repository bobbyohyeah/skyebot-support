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
import concurrent.futures

load_dotenv()

# --- Argument Parsing ---
def parse_arguments():
    parser = argparse.ArgumentParser(description="Generate a response to a customer inquiry using Google Drive resources and GenAI.")
    parser.add_argument("-i", "--inquiry", help="The customer inquiry text (will prompt if not provided).")
    parser.add_argument("-d", "--download", action="store_true", help="Download files from Google Drive (otherwise use local files).")
    parser.add_argument("-m", "--model", choices=['flash', 'pro'], default='flash', help="Specify the GenAI model to use ('flash' or 'pro'). Defaults to 'flash'.")
    args = parser.parse_args()
    return args

# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

def get_drive_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
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
        local_filename = f"{local_filename_base}.txt" # Default extension

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
        name: os.path.join(download_dir, f"{name}.csv" if name == "Links" else f"{name}.txt")
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
    # if download_flag: # Original condition
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
                print(f"Finished processing '{name}'. Saved to {downloaded_path}")
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

    # --- Upload Files to GenAI API --- (Do this ONCE)
    print("--- Uploading Files to GenAI API --- ")
    genai_file_parts = []

    def upload_single_file(name, file_path):
        try:
            print(f"Uploading {file_path} ({name}) to GenAI API...")
            genai_file = client.files.upload(file=file_path)
            print(f"Successfully uploaded {name} ({genai_file.name})")
            return types.Part.from_uri(file_uri=genai_file.uri, mime_type=genai_file.mime_type)
        except Exception as e:
             print(f"Failed to upload {file_path} ({name}) to GenAI: {e}")
             return None # Return None on failure

    # Use ThreadPoolExecutor for concurrent uploads
    uploaded_parts = []
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Create a future for each file upload
        future_to_name = {executor.submit(upload_single_file, name, path): name for name, path in processed_files_paths.items()}
        for future in concurrent.futures.as_completed(future_to_name):
            result = future.result()
            if result: # Only add successful uploads
                uploaded_parts.append(result)

    genai_file_parts = uploaded_parts # Assign the collected parts

    if not genai_file_parts:
        print("No files were successfully uploaded to GenAI. Aborting.")
        return client, None # Return client in case user wants to try generation without files?

    print(f"Using Gemini {model_name}")
    return client, genai_file_parts


def generate_response(client, file_parts, inquiry_text, model_name):
    """Generates a response for a single inquiry using pre-uploaded files."""
    print("\n📤 Inquiry\n")
    print(inquiry_text + "\n")
    print("\n🔍 Response\n")

    last_usage_metadata = None
    response_text = ""

    if not client:
        print("GenAI client is not available.")
        return None

    try:
        # Use the model_name argument passed to the function
        model_str = "models/gemini-2.0-flash" if model_name == 'flash' else "models/gemini-2.5-pro-exp-03-25"

        # Construct parts list including the pre-uploaded files and the new inquiry
        current_parts = []
        if file_parts: # Add file parts if they exist
            current_parts.extend(file_parts)
        current_parts.append(types.Part.from_text(text=inquiry_text))

        contents = [types.Content(role="user", parts=current_parts)]
        generate_content_config = types.GenerateContentConfig(
            response_mime_type="text/plain",
            system_instruction=[
                types.Part.from_text(text="""You are a customer support specialist for SkyeBrowse. Use the provided resources when necessary to assist the customer with their inquiry. If there is a link that would be beneficial for customer, provide the link. The customer has sent an email, so you are to write an email response back. Write the email in complete paragraphs. Be direct and straight to the point. Do not refer to support in the third person, since you are supporting the customer. Instead of "contact support with your email", say "send me your email". Again, do NOT use bullet points in your email.
                    Here is an example of a customer inquiry and response format.
                    Customer Inquiry:
                    Name: Howard
                    Company: AerialClicks.com
                    Title: Owner
                    Phone: 12068186250
                    Message: Is the Air2s compatible using the RC Pro controller?

                    Response:
                    Howard,

                    The Air2S should be supported from this link: https://play.google.com/store/apps/details?id=com.skyebrowse.android&pli=1. Alternatively, you can also manually record a video and upload it using the Universal Upload option."""),
            ],
        )

        stream = client.models.generate_content_stream(
            model=model_str, # Use the selected model string
            contents=contents,
            config=generate_content_config,
        )

        for chunk in stream:
            # Check if chunk.text has content before processing
            if chunk.text:
                print(chunk.text, end="")
                response_text += chunk.text # Accumulate text
            if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                 last_usage_metadata = chunk.usage_metadata # Capture the latest usage metadata

    except Exception as e:
        print(f"\nAn error occurred during generation: {e}")
        # last_usage_metadata remains as it was

    return last_usage_metadata

if __name__ == "__main__":
    args = parse_arguments()
    model_name = args.model

    # Prepare files and client ONCE, passing the original download flag
    genai_client, prepared_file_parts = prepare_context_files(args.download)

    if not genai_client:
        print("Exiting due to issue with GenAI client initialization.")
        exit()

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

        # Process this single inquiry
        start_time = time.time()
        final_usage_metadata = generate_response(genai_client, prepared_file_parts, inquiry_to_use, args.model)
        end_time = time.time()
        duration = end_time - start_time

        # Print summary for this inquiry
        print(f"\n\n=== Summary ===\n")
        print(f"Time taken: {duration:.2f} seconds")
        if final_usage_metadata:
            print(f"Input Tokens: {final_usage_metadata.prompt_token_count}")
            print(f"Output Tokens: {final_usage_metadata.candidates_token_count}")
            print(f"Total Tokens: {final_usage_metadata.total_token_count}")
        else:
            print("Token usage metadata not available for this inquiry.")
        print("=======================")
