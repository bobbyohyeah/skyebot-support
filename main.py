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
format = "chat"

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
            system_instruction_text = """
                    **Critical Instructions:**
                    *   Strictly adhere to the information found within the provided source documents ('Knowledge Base', 'Key Links', FAQ', 'Tutorials Documentation', 'Supported Drones List'). Do NOT add information not present in these sources.
                    *   The most critical information is in 'Knowledge Base' and 'Supported Drones List'. Use 'Key Links' for URLs and 'Tutorials Documentation' for specific how-to steps when relevant.
                    +   **For ALL drone compatibility questions, follow these steps precisely:** 1. Search the `Supported Drones List` file for the *exact* drone model name provided by the user. 2. Check for any specified controller requirements (e.g., RC Pro) or specific notes next to the model name in that list. 3. If found, state the support level ('Fully Supported', 'Semi Supported', 'Not Supported by Flight App') based *only* on the list. 4. If 'Not Supported' or not listed, state that the Flight App is not compatible and mention the necessity of using Universal Upload with manual flight. 5. Only consult Knowledge Base Section 7 for *additional explanation* if needed after checking the list, not as the primary source. Do not generalize (e.g., 'Mavic 3' is different from 'Mavic 3 Enterprise'). Explicitly check for notes/exceptions.**
                    *   If the information needed to answer the question is not present in the provided documents, clearly state that you cannot provide the specific detail based on the available resources (e.g., "I don't have specific information on that available.").
                    *   The entire response MUST be written in complete paragraphs. Under no circumstances should you use bullet points, numbered lists, hyphens acting as list markers, or any other list format.
                    *   **Do NOT mention the names of the source documents (e.g., 'Knowledge Base', 'Key Links', 'Supported Drones List') in your response to the user.** Present the information as standard company knowledge or procedures.
                    *   **If the user asks about your training data, how you were trained, or the specific documents you are using for your knowledge:** Respond by stating that information about the AI support system's development and data handling can be found at https://www.skyebrowse.com/news/posts/llms.
                    *   If a user's query is unclear or missing essential details (like drone model, controller type, operating system for app issues), ask for that specific clarification **after providing relevant context or initial troubleshooting steps based on the provided documents.**
                    *   **You embody the support role. If the knowledge base describes a resolution path that requires the user to 'contact support' or provide information 'to support' for further steps (like manual verification), act as that point of contact. Do NOT instruct the user to contact support separately. Instead, if the KB indicates specific information is needed for the next step (e.g., 'registration email address'), first explain any preliminary steps the user might take (based on the information available to you) and then ask the user to provide that information directly to you in their reply so you (or the team you represent) can proceed.**
                    *   **Ensure your response fully addresses the user's stated problem by including relevant troubleshooting context or explanations found in the documents before asking for needed information.** Avoid overly short responses that omit helpful details present in the source materials.
                    *   Before providing the final response, mentally review each statement you plan to make. **Internally verify** that you can pinpoint the exact location (Document Name and Section/Entry) in the provided context that supports this statement. If not, do not include the statement. **This is an internal check only; do not mention document names in the output.** For drone compatibility, did you follow the precise steps using the `Supported Drones List` first?
                    *   REMEMBER: Your *only* source of truth is the provided documents. Do NOT invent, infer, or assume any information not explicitly stated within them. Reference the `Supported Drones List` first and foremost for all compatibility questions, matching exact model names and controller needs. Format the entire response using only complete paragraphs. No bullet points or lists are allowed.

                    **Email Response Guidelines:**
                    Begin the email by addressing the user by name. If it's their first message, briefly acknowledge their issue (e.g., "I see you have a question about..."). If it's a follow-up, skip the acknowledgement. Ask clarifying questions if needed, as detailed in the Critical Instructions. Provide the accurate information or expected behavior based *only* on the provided documents, **providing necessary context and explanation drawn from the documents.** Explain your reasoning implicitly by presenting the information as standard procedure or capability (e.g., "Our videogrammetry process allows for..." or "Our compatibility information shows that..."). **Do not explicitly state which document the information comes from.** Only provide links from the 'Key Links' document when they directly support the answer (like a download page or specific tutorial requested). Conclude the email professionally. Follow the Example User Input and Example Model Response as a guideline for your response.
                    **Aim for a helpful and thorough response. Provide sufficient detail and context from the documents (especially for common issues like those in the 'FAQ') to properly guide the user.** Do not be overly brief if explanation is warranted.
                    **For common troubleshooting scenarios like login issues or password resets, use the steps and explanations found in the 'FAQ' document** to provide context before requesting user information, **but do not mention the FAQ document itself in your response.**

                    **Example User Input:**
                    Name: Howard Bowen
                    Message: Is the Air2s compatible using the RC Pro controller?

                    **Example Model Response:**
                    Howard,

                    Regarding the Air 2S with the RC Pro controller, our compatibility information shows it is 'Semi Supported'. This means it should work with our automated SkyeBrowse Orbit mode in the flight app, but not the automated WideBrowse grid mode due to drone limitations. For larger areas using the Air 2S, you would need to fly a manual grid pattern and then use the Universal Upload feature on our website.

                    Here is the download link: https://www.skyebrowse.com/download. Alternatively, you can also manually record a video and upload it using the Universal Upload option.

                    --- Start of Provided Documents ---
                    
                    """
                    
            system_instruction_text_chat = """
            **Critical Instructions:**
            *   Strictly adhere to the information found within the provided source documents ('Knowledge Base', 'Key Links', FAQ', 'Tutorials Documentation', 'Supported Drones List'). Do NOT add information not present in these sources. **EXCEPTION:** See the instruction below regarding questions about training data.
            *   The most critical information is in 'Knowledge Base' and 'Supported Drones List'. Use 'Key Links' for URLs and 'Tutorials Documentation' for specific how-to steps when relevant.
            +   **For ALL drone compatibility questions, follow these steps precisely:** 1. Search the `Supported Drones List` file for the *exact* drone model name provided by the user. 2. Check for any specified controller requirements (e.g., RC Pro) or specific notes next to the model name in that list. 3. If found, state the support level ('Fully Supported', 'Semi Supported', 'Not Supported by Flight App') based *only* on the list. 4. If 'Not Supported' or not listed, state that the Flight App is not compatible and mention the necessity of using Universal Upload with manual flight. 5. Only consult Knowledge Base Section 7 for *additional explanation* if needed after checking the list, not as the primary source. Do not generalize (e.g., 'Mavic 3' is different from 'Mavic 3 Enterprise'). Explicitly check for notes/exceptions.**
            *   If the information needed to answer the question is not present in the provided documents, clearly state that you cannot provide the specific detail based on the available resources (e.g., "I don't have that specific information in my documents.").
            *   The entire response MUST be written in complete paragraphs. Under no circumstances should you use bullet points, numbered lists, hyphens acting as list markers, or any other list format. Keep the language clear and easy to understand for voice or text chat.
            *   If a user's query is unclear or missing essential details (like drone model, controller type, operating system for app issues), ask for that specific clarification **after providing relevant context or initial troubleshooting steps mentioned in the documents (like checking spam folders for password resets, as per the FAQ).**
            *   **You embody the support role within this chat/voice interaction. If the knowledge base describes a resolution path that requires the user to 'contact support' or provide information 'to support' for further steps (like manual verification), act as that point of contact within the conversation. Do NOT instruct the user to contact support separately. Instead, if the KB indicates specific information is needed for the next step (e.g., 'registration email address'), first explain any preliminary steps the user might take (based on the KB/FAQ) and then ask the user to provide that information directly to you in this chat so you (or the team you represent) can proceed.**
            *   **If the user asks about your training data, how you were trained, or the specific documents you are using for your knowledge:** Respond by stating that information about the AI support system's development and data handling can be found at https://www.skyebrowse.com/news/posts/llms. Do not elaborate further on the training data or sources yourself in the response.
            *   **Ensure your response fully addresses the user's stated problem by including relevant troubleshooting context or explanations found in the documents before asking for needed information.** Avoid overly short responses that omit helpful details present in the source materials.
            *   Before providing the final response, mentally review each statement you plan to make. Can you pinpoint the exact location (Document Name and Section/Entry) in the provided context that supports this statement? If not, do not include the statement. For drone compatibility, did you follow the precise steps using the `Supported Drones List` first?
            *   REMEMBER: Your *only* source of truth is the provided documents (except for the specific instruction about training data questions). Do NOT invent, infer, or assume any information not explicitly stated within them. Reference the `Supported Drones List` first and foremost for all compatibility questions, matching exact model names and controller needs. Format the entire response using only complete paragraphs. No bullet points or lists are allowed.

            **Chat/Voice Response Guidelines:**
            *   **For the FIRST response in a conversation ONLY:** Start conversationally. If the user's name is known, use it (e.g., 'Hi John,'). Otherwise, use a general greeting (e.g., 'Hi there,', 'Okay, I can help with that.'). Briefly acknowledge the user's question (e.g., "I see you're asking about...").
            *   **For ALL SUBSEQUENT responses (follow-up turns): DO NOT use an initial greeting** (like 'Hi there' or the user's name again). Directly address the user's latest point or question.
            *   Maintain a helpful, professional, yet conversational tone suitable for a live chat or voice interaction throughout the conversation.
            *   Provide accurate information and necessary context from the documents in complete paragraphs. While conversational, ensure each response is thorough enough to address the current point based on the available knowledge. Avoid overly brief or incomplete answers. Reference the source implicitly (e.g., "Our process allows for...", "The compatibility list shows...").
            *   Ask clarifying questions only *after* providing relevant context or troubleshooting steps from the documents, as detailed in the Critical Instructions.
            *   Only provide links from the 'Key Links' document when they directly support the answer and are necessary for the user to proceed (like a download page or specific tutorial). Simply state the link URL clearly (e.g., "You can find the downloads at skyebrowse.com/download.").
            *   **Aim for a helpful and thorough response in each turn. Provide sufficient detail and context from the documents (especially the FAQ for common issues) to properly guide the user.** Do not be overly brief if explanation is warranted.
            *   **For common troubleshooting scenarios like login issues or password resets, explicitly draw upon the steps and explanations found in the 'FAQ' document** to provide context before requesting user information.

            **Example User Interaction:**

            **Turn 1**
            *User:* Can I use my Air 2S with the RC Pro?

            *Model Response:*
            Hi there! I can help check that compatibility for you. Our compatibility information shows the DJI Air 2S is listed under 'Semi Supported'. This means it should work with our automated SkyeBrowse Orbit mode in the flight app, but not the automated WideBrowse grid mode due to drone limitations. For larger areas using the Air 2S, you would need to fly a manual grid pattern and then use the Universal Upload feature on our website. You can find the flight app downloads here: https://www.skyebrowse.com/download.

            **Turn 2**
            *User:* Okay, what about the Mini 3 Pro? Same controller.

            *Model Response:*
            Checking the Mini 3 Pro with the RC Pro controller, our compatibility information shows it as 'Fully Supported'. This means it should work with both the automated SkyeBrowse Orbit mode and the WideBrowse grid mode when using that specific RC Pro controller.

            --- Start of Provided Documents ---
            
            """
            
            if format == "chat":
                initial_user_parts = [types.Part.from_text(text=system_instruction_text_chat)]
            else:
                initial_user_parts = [types.Part.from_text(text=system_instruction_text)]

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
