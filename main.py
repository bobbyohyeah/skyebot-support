from dotenv import load_dotenv
import os
import io
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

    os.makedirs(download_dir, exist_ok=True)

    if download_flag:
        print("--- Downloading Files from Google Drive ---")
        drive_service = get_drive_service()
        for name, file_id in file_ids.items():
            local_filename_base = os.path.join(download_dir, name)
            print(f"Processing '{name}' from Google Drive (ID: {file_id})...")
            downloaded_path = download_google_doc(drive_service, file_id, local_filename_base)
            if downloaded_path:
                processed_files_paths[name] = downloaded_path
                print(f"Finished processing '{name}'. Saved to {downloaded_path}")
            else:
                print(f"Skipped file '{name}' due to download/conversion issue.")
    else:
        print("--- Skipping Google Drive Download - Using Local Files ---")
        for name, expected_path in expected_local_files.items():
            if os.path.exists(expected_path):
                print(f"Found local file: {expected_path}")
                processed_files_paths[name] = expected_path
            else:
                print(f"Local file not found: {expected_path}. It will not be used.")

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

    print("--- Context File Preparation Complete --- ")
    return client, genai_file_parts


def generate_response(client, file_parts, inquiry_text):
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
        # model = "gemini-2.0-flash" # this is for support chat bot
        model = "gemini-2.5-pro-exp-03-25" # this is for email chat bot

        # Construct parts list including the pre-uploaded files and the new inquiry
        current_parts = []
        if file_parts: # Add file parts if they exist
            current_parts.extend(file_parts)
        current_parts.append(types.Part.from_text(text=inquiry_text))

        contents = [types.Content(role="user", parts=current_parts)]
        generate_content_config = types.GenerateContentConfig(
            response_mime_type="text/plain",
            system_instruction=[
                types.Part.from_text(text="""You are a customer support specialist for SkyeBrowse. Use the provided resources when necessary to assist the customer with their inquiry. If there is a link that would be beneficial for customer, provide the link. The customer has sent an email, so you are to write an email response back. Write the email in complete paragraphs. Be direct and straight to the point. If the question is vague, ask for clarification. Again, do NOT use bullet points in your email."""),
            ],
        )

        stream = client.models.generate_content_stream(
            model=model,
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

    # Prepare files and client ONCE
    genai_client, prepared_file_parts = prepare_context_files(args.download)

    if not genai_client:
        print("Exiting due to issue with GenAI client initialization.")
        exit()
    # We might allow proceeding even if file_parts is None/empty, depending on desired behavior
    # if not prepared_file_parts:
    #     print("Warning: No context files were successfully uploaded. Proceeding without file context.")

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
        final_usage_metadata = generate_response(genai_client, prepared_file_parts, inquiry_to_use)
        end_time = time.time()
        duration = end_time - start_time

        # Print summary for this inquiry
        print(f"\n=== Summary ===\n")
        print(f"Time taken: {duration:.2f} seconds")
        if final_usage_metadata:
            print(f"Input Tokens: {final_usage_metadata.prompt_token_count}")
            print(f"Output Tokens: {final_usage_metadata.candidates_token_count}")
            print(f"Total Tokens: {final_usage_metadata.total_token_count}")
        else:
            print("Token usage metadata not available for this inquiry.")
        print("=======================")
