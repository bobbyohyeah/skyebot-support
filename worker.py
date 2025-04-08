from flask import Flask, request, jsonify
import os
import io
import concurrent.futures
import threading
import shutil # Added for directory deletion
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai
from google.genai import types
from googleapiclient.errors import HttpError
import logging
import argparse # Added for command-line arguments

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv(override=True)

# --- Global Variables for GenAI Client and Files ---
genai_client = None
prepared_file_parts = None
initialization_lock = threading.Lock()
is_initialized = False
download_files_on_init = False # Global flag to hold arg value
selected_model_name = 'flash' # Default model - flash or pro

# --- Constants ---
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DOWNLOAD_DIR = "drive_worker" # Use a separate directory for worker

# --- Argument Parsing (similar to main.py) ---
def parse_arguments():
    parser = argparse.ArgumentParser(description="Run the Gemini Cloudflare Worker with optional file download.")
    parser.add_argument("-d", "--download", action="store_true", help="Download files from Google Drive on startup.")
    parser.add_argument("-m", "--model", choices=['flash', 'pro'], default='flash', help="Specify the GenAI model to use ('flash' or 'pro'). Defaults to 'flash'.")
    # Add other worker-specific args here if needed
    args = parser.parse_args()
    return args

# --- Google Drive Functions (Adapted from main.py) ---
def get_drive_service():
    creds = None
    token_path = "token.json" # Use separate token for worker
    credentials_path = "credentials.json"
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logger.warning(f"Failed to load credentials from {token_path}: {e}. Will attempt re-authentication.")
            creds = None # Force re-authentication
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired Google Drive token.")
                creds.refresh(Request())
            except Exception as e:
                logger.error(f"Failed to refresh token: {e}. Need new authorization.")
                creds = None # Force re-authentication
        if not creds: # If still no valid creds, run flow
            try:
                logger.info("Attempting new Google Drive authorization flow.")
                if not os.path.exists(credentials_path):
                    logger.error(f"Error: {credentials_path} not found. Cannot authorize.")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                # Note: run_local_server will require user interaction in the console where the worker runs
                creds = flow.run_local_server(port=0)
            except Exception as e:
                logger.error(f"Failed to run authorization flow: {e}")
                return None
        # Save the credentials for the next run
        if creds:
            try:
                with open(token_path, "w") as token:
                    token.write(creds.to_json())
                logger.info(f"Google Drive credentials saved to {token_path}")
            except Exception as e:
                logger.error(f"Failed to save token to {token_path}: {e}")

    if not creds:
         logger.error("Failed to obtain valid Google Drive credentials.")
         return None

    try:
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Failed to build Drive service: {e}")
        return None

def download_google_doc(service, file_id, local_filename_base):
    try:
        file_metadata = service.files().get(fileId=file_id, fields='mimeType, name').execute()
        mime_type = file_metadata.get('mimeType')
        original_name = file_metadata.get('name')
        logger.info(f"  File '{original_name}' has MIME type: {mime_type}")
        request = None
        local_filename = f"{local_filename_base}.txt"
        if mime_type == 'application/vnd.google-apps.document':
            logger.info(f"  Exporting Google Doc '{original_name}' as text/plain...")
            request = service.files().export_media(fileId=file_id, mimeType="text/plain")
        elif mime_type == 'text/plain':
            logger.info(f"  Downloading plain text file '{original_name}' directly...")
            request = service.files().get_media(fileId=file_id)
        elif mime_type == 'application/vnd.google-apps.spreadsheet':
            logger.info(f"  Exporting Google Sheet '{original_name}' as text/csv...")
            request = service.files().export_media(fileId=file_id, mimeType="text/csv")
            local_filename = f"{local_filename_base}.csv"
        else:
            logger.warning(f"  Skipping file '{original_name}' (ID: {file_id}). Unsupported MIME type: {mime_type}")
            return None
        if request:
            fh = io.FileIO(local_filename, "wb")
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                if status:
                    logger.debug(f"  Download {int(status.progress() * 100)}%.")
            fh.close()
            logger.info(f"  Successfully downloaded/exported to {local_filename}")
            return local_filename
        else:
             logger.error(f"  Could not create download request for file '{original_name}' (ID: {file_id})")
             return None
    except HttpError as error:
        logger.error(f"  An HTTP error occurred downloading file ID {file_id}: {error}")
        return None
    except Exception as e:
        logger.error(f"  A general error occurred downloading file ID {file_id}: {e}")
        return None

# --- GenAI Functions (Adapted from main.py) ---
def prepare_context_files_and_client(download_flag):
    global genai_client, prepared_file_parts, is_initialized
    with initialization_lock:
        if is_initialized:
            logger.info("Initialization already performed.")
            return

        logger.info("--- Initializing GenAI Client and Preparing Context Files ---")
        file_ids = {}
        prefix = "GDRIVE_"
        for key, value in os.environ.items():
            if key.startswith(prefix):
                name_parts = key[len(prefix):].split('_')
                name = ' '.join(part.capitalize() for part in name_parts)
                file_ids[name] = value

        if not file_ids:
            logger.error("ERROR: No Google Drive file IDs found in environment variables (e.g., GDRIVE_XYZ=...). Cannot prepare context.")
            is_initialized = True # Mark as initialized (even though failed) to prevent retries
            return

        logger.info(f"Loaded {len(file_ids)} file IDs from environment.")
        processed_files_paths = {}

        # --- Check Directory Status ---
        dir_exists = os.path.exists(DOWNLOAD_DIR)
        # Check if directory is empty, handle case where it doesn't exist yet
        dir_is_empty = True
        if dir_exists:
            try:
                if not os.listdir(DOWNLOAD_DIR):
                    dir_is_empty = True
                else:
                    dir_is_empty = False
            except OSError as e:
                logger.warning(f"Could not check if directory {DOWNLOAD_DIR} is empty: {e}. Assuming it might not be.")
                dir_is_empty = False # Play safe if we can't list contents

        logger.info(f"Directory '{DOWNLOAD_DIR}' exists: {dir_exists}, Is empty: {dir_is_empty}")

        # --- Determine if Download is Necessary ---
        # Download if flag is set OR directory doesn't exist OR directory is empty
        should_download = download_flag or not dir_exists or dir_is_empty
        if should_download and not download_flag:
            if not dir_exists:
                logger.info(f"Directory '{DOWNLOAD_DIR}' not found. Files will be downloaded.")
            elif dir_is_empty:
                logger.info(f"Directory '{DOWNLOAD_DIR}' is empty. Files will be downloaded.")

        # --- Clean directory ONLY if download flag is explicitly set ---
        if download_flag:
            logger.info(f"Download flag is set. Attempting to clean directory: {DOWNLOAD_DIR}")
            if dir_exists:
                try:
                    shutil.rmtree(DOWNLOAD_DIR)
                    logger.info(f"Successfully deleted existing directory: {DOWNLOAD_DIR}")
                    dir_exists = False # Update status after deletion
                    dir_is_empty = True
                except OSError as e:
                    logger.error(f"Error deleting directory {DOWNLOAD_DIR}: {e}. Proceeding may result in outdated files.")
            else:
                logger.info(f"Directory {DOWNLOAD_DIR} did not exist, no deletion needed.")

        # --- Ensure download directory exists before proceeding ---
        # Needed if it didn't exist initially OR if it was deleted above
        if not dir_exists:
            try:
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                logger.info(f"Ensured download directory exists: {DOWNLOAD_DIR}")
            except OSError as e:
                logger.error(f"Error creating directory {DOWNLOAD_DIR}: {e}. Cannot proceed.")
                is_initialized = True # Mark as initialized (failed)
                return

        # --- Determine expected local files (needed for both scenarios) ---
        expected_local_files = {
            name: os.path.join(DOWNLOAD_DIR,
                                f"{name.replace(' ', '_')}.csv" if name == "Links" else f"{name.replace(' ', '_')}.txt")
            for name in file_ids
        }

        # --- Download or Use Local Files ---
        if should_download:
            # This block runs if -d flag OR dir was missing/empty
            logger.info("--- Downloading Files from Google Drive for Worker ---")
            drive_service = get_drive_service()
            if not drive_service:
                logger.error("Failed to get Google Drive service. Cannot download files.")
                is_initialized = True
                return

            for name, file_id in file_ids.items():
                # Use the expected path base for consistency
                local_filename_base = os.path.splitext(expected_local_files[name])[0]
                logger.info(f"Processing '{name}' from Google Drive (ID: {file_id})...")
                downloaded_path = download_google_doc(drive_service, file_id, local_filename_base)
                if downloaded_path:
                    processed_files_paths[name] = downloaded_path
                    logger.info(f"Finished processing '{name}'. Saved to {downloaded_path}")
                else:
                    logger.warning(f"Skipped file '{name}' due to download/conversion issue.")
        else:
            # This block now only runs if download_flag is False AND dir existed AND dir was not empty
            logger.info("--- Skipping Google Drive Download - Using Existing Local Files ---")
            for name, expected_path in expected_local_files.items():
                if os.path.exists(expected_path):
                    logger.info(f"Found local file: {expected_path}")
                    processed_files_paths[name] = expected_path
                else:
                    # This case should be less likely if the dir wasn't empty, but good to log
                    logger.warning(f"Expected local file not found: {expected_path}. It will not be used for context.")

        # --- Proceed with GenAI Client Init and Upload ---
        if not processed_files_paths:
             logger.warning("No files were successfully downloaded/found. Proceeding without file context.")
             # Still initialize client below, but file_parts will be empty

        # --- Initialize GenAI Client ---
        logger.info("--- Initializing GenAI Client ---")
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.error("GEMINI_API_KEY not found in environment variables.")
            is_initialized = True
            return
        try:
            # Use genai.configure() for API key and then Client()
            genai_client = genai.Client(api_key=api_key) # Correct initialization
            # Test connection (optional but recommended)
            # list(genai_client.models.list())
            logger.info("GenAI Client Initialized Successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize GenAI client: {e}")
            is_initialized = True
            return # Cannot proceed without client

        # --- Upload Files to GenAI API ---
        logger.info("--- Uploading Files to GenAI API ---")
        uploaded_parts = []
        if processed_files_paths: # Only upload if we have paths
            def upload_single_file(name, file_path):
                try:
                    logger.info(f"Uploading {file_path} ({name}) to GenAI API...")
                    genai_file = genai_client.files.upload(file=file_path) # Use global client
                    logger.info(f"Successfully uploaded {name} ({genai_file.name})")
                    return types.Part.from_uri(file_uri=genai_file.uri, mime_type=genai_file.mime_type)
                except Exception as e:
                     logger.error(f"Failed to upload {file_path} ({name}) to GenAI: {e}")
                     return None

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future_to_name = {executor.submit(upload_single_file, name, path): name for name, path in processed_files_paths.items()}
                for future in concurrent.futures.as_completed(future_to_name):
                    result = future.result()
                    if result:
                        uploaded_parts.append(result)
        else:
            logger.info("Skipping GenAI file upload as no files were processed.")

        prepared_file_parts = uploaded_parts # Assign to global
        if not prepared_file_parts:
            logger.warning("No files were successfully uploaded to GenAI. Responses will lack file context.")
        else:
             logger.info(f"Successfully uploaded {len(prepared_file_parts)} files to GenAI.")

        logger.info("--- Context File Preparation and Client Initialization Complete ---")
        is_initialized = True # Mark initialization as complete


def generate_response_for_webhook(inquiry_text):
    """Generates a response using the pre-initialized client and files."""
    logger.info(f"Generating response for inquiry: {inquiry_text[:50]}...") # Log truncated inquiry
    if not is_initialized or not genai_client:
        logger.error("GenAI client not initialized. Cannot generate response.")
        return "Error: Service not ready.", 503 # Service Unavailable

    response_text = ""
    try:
        # Use the globally selected model name
        model_str = "models/gemini-2.0-flash" if selected_model_name == 'flash' else "models/gemini-2.5-pro-exp-03-25"
        print(f"Using model: {model_str}")

        current_parts = []
        if prepared_file_parts: # Use the globally prepared file parts
            current_parts.extend(prepared_file_parts)
        current_parts.append(types.Part.from_text(text=inquiry_text))
        contents = [types.Content(role="user", parts=current_parts)]

        # Reuse system instruction from main.py
        system_instruction_text = """You are a customer support specialist for SkyeBrowse. Use the provided resources when necessary to assist the customer with their inquiry. If there is a link that would be beneficial for customer, provide the link. The customer has sent an email, so you are to write an email response back. Write the email in complete paragraphs. Be direct and straight to the point. Again, do NOT use bullet points in your email.
                    Here is an example of a customer inquiry and response format.
                    Customer Inquiry:

                    Name: Howard
                    Company: AerialClicks.com
                    Title: Owner
                    Phone: 12068186250
                    Message: Is the Air2s compatible using the RC Pro controller?

                    Response:
                    Howard,

                    The Air2S should be supported from this link: https://play.google.com/store/apps/details?id=com.skyebrowse.android&pli=1. Alternatively, you can also manually record a video and upload it using the Universal Upload option."""

        generate_content_config = types.GenerateContentConfig(
            response_mime_type="text/plain",
            system_instruction=[types.Part.from_text(text=system_instruction_text)],
            # Removed temperature as it might not be needed for direct responses
        )

        # Using generate_content for simpler webhook response (no streaming needed)
        response = genai_client.models.get(model_str).generate_content(
             contents=contents,
             config=generate_content_config,
        )

        # Extract text safely
        if response.candidates and response.candidates[0].content.parts:
             response_text = response.candidates[0].content.parts[0].text
        else:
             response_text = "No response generated."
             logger.warning("Received empty response from GenAI.")

        # Log token usage if available
        if response.usage_metadata:
             logger.info(f"GenAI Token Usage: Input={response.usage_metadata.prompt_token_count}, Output={response.usage_metadata.candidates_token_count}, Total={response.usage_metadata.total_token_count}")

    except Exception as e:
        logger.error(f"An error occurred during GenAI generation: {e}", exc_info=True)
        # Return a user-friendly error, but log the details
        return f"Error generating response: {type(e).__name__}", 500 # Internal Server Error

    logger.info("Response generated successfully.")
    return response_text, 200 # OK

# --- Flask App ---
app = Flask(__name__)

def initialize(download_flag, model_name):
    """Function to run before the first request to initialize everything."""
    global selected_model_name # Declare intent to modify global
    selected_model_name = model_name # Store the selected model name
    logger.info(f"Flask starting up - performing initialization with model: {selected_model_name}...")
    prepare_context_files_and_client(download_flag)
    logger.info("Initialization attempt complete.") # Log regardless of success

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    logger.info("Received request at /webhook")
    if not is_initialized:
        logger.warning("Initialization not yet complete. Returning 503.")
        return jsonify({"error": "Service initializing, please try again shortly."}), 503

    if not genai_client:
         logger.error("GenAI client is not available after initialization attempt. Returning 500.")
         return jsonify({"error": "Service configuration error."}), 500


    if not request.is_json:
        logger.warning("Request is not JSON.")
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    inquiry = data.get('inquiry')

    if not inquiry:
        logger.warning("Missing 'inquiry' field in JSON payload.")
        return jsonify({"error": "Missing 'inquiry' field in request body"}), 400

    # Generate the response using the core logic
    response_content, status_code = generate_response_for_webhook(inquiry)

    if status_code == 200:
        return jsonify({"response": response_content}), status_code
    else:
        # Handle errors reported by the generation function
        return jsonify({"error": response_content}), status_code

@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    if is_initialized and genai_client:
        return jsonify({"status": "OK", "initialized": True}), 200
    elif is_initialized and not genai_client:
         return jsonify({"status": "Error", "initialized": True, "message": "Initialization complete but client unavailable."}), 500
    else:
        return jsonify({"status": "Initializing", "initialized": False}), 503


if __name__ == '__main__':
    # Run initialization directly when script starts if not using a WSGI server like gunicorn
    # For production, use gunicorn: gunicorn -w 4 worker:app
    # For development: python worker.py [-d]
    args = parse_arguments()
    if not os.environ.get("FLASK_RUN_FROM_CLI"): # Avoid double init if using `flask run`
         initialize(args.download, args.model)

    # Consider PORT from environment variable for flexibility
    port = int(os.environ.get("PORT", 8081)) # Use 8081 to avoid conflict
    # Use threaded=True for handling multiple requests during development
    # Use a proper WSGI server (like gunicorn or waitress) for production
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

    # --- Cloudflare Tunnel Instructions ---
    # 1. Install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/
    # 2. Login: `cloudflared login`
    # 3. Create a tunnel (do this once): `cloudflared tunnel create my-genai-webhook`
    #    (Note the tunnel ID and credentials file path)
    # 4. Configure the tunnel to point to your local service in the credentials file (~/.cloudflared/TUNNEL_ID.json or specified path):
    #    Add/update the 'url' key in the 'ingress' section:
    #    "ingress": [
    #      {
    #        "hostname": "YOUR_DESIRED_HOSTNAME.yourdomain.com", # Or use the random subdomain provided if you don't have a domain
    #        "service": "http://localhost:8081" # Match the port Flask runs on
    #      },
    #      {
    #        "service": "http_status:404" # Catch-all rule
    #      }
    #    ], ...
    #    (Alternatively, use Quick Tunnels for temporary testing without config: `cloudflared tunnel --url http://localhost:8081`)
    # 5. Run the tunnel: `cloudflared tunnel run my-genai-webhook` (replace with your tunnel name/ID)
    #
    # Your webhook URL will be https://YOUR_ASSIGNED_CLOUDFLARE_URL/webhook
    # Send POST requests to this URL with JSON body: {"inquiry": "Your customer question here"} 