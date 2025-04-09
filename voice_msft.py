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
import asyncio
import edge_tts
import tempfile
import sounddevice as sd
import soundfile as sf
from google.cloud import speech
extension = "txt"
format = "voice" # voice or chat or email

load_dotenv(override=True)

# --- Audio Recording Settings ---
SAMPLE_RATE = 16000 # Sample rate for recording (Hz) - common for STT
CHANNELS = 1       # Mono audio
AUDIO_FILENAME = "temp_input_audio.wav"

# --- Edge TTS Settings ---
VOICE = "en-US-AvaNeural" # Example voice
SENTENCES_PER_CHUNK = 4 # Target sentences per playback chunk
# --- Sentence End Detection ---
SENTENCE_ENDS = {'.', '?', '!'}

# --- afplay Audio Playback Function (Async) ---
async def play_audio_chunk_async(audio_data):
    if not audio_data:
        print("Warning: No audio data received to play.")
        return
    temp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_audio_file:
            temp_audio_file.write(audio_data)
            temp_file_path = temp_audio_file.name
        process = await asyncio.create_subprocess_exec(
            "afplay",
            temp_file_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            error_message = stderr.decode().strip() if stderr else 'Unknown error'
            print(f"\n[afplay error: {error_message}]")
    except Exception as e:
        print(f"\n[Error during audio playback: {e}]")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError as e:
                print(f"\n[Warning: Could not remove temp file {temp_file_path}: {e}]")

# --- Edge TTS Streaming Function (Async) ---
async def synthesize_text_async(text, voice=VOICE):
    """Synthesizes the given text using edge-tts and *returns* the audio data bytes."""
    if not text:
        print("No text provided for TTS synthesis.")
        return None # Return None if no text
    print(f"\n[Synthesizing TTS for text starting with: '{text[:50]}...']")
    audio_buffer = bytearray()
    try:
        communicate = edge_tts.Communicate(text, voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_buffer.extend(chunk["data"])

        if not audio_buffer:
            print(f"\n[Warning: No audio generated during synthesis for text '{text[:50]}...']")
            return None

        print(f"\n[Finished TTS for '{text[:50]}...']")
        return bytes(audio_buffer) # Return the synthesized bytes

    except Exception as e:
        print(f"\n[Error during TTS synthesis for text '{text[:50]}...': {e}]")
        return None # Return None on error

    # Removed playback call

# --- Audio Recording Function ---
def record_audio(filename=AUDIO_FILENAME, duration=5, samplerate=SAMPLE_RATE, channels=CHANNELS):
    """Records audio from the microphone for a specified duration."""
    print(f"Recording for {duration} seconds... Speak now!")
    try:
        recording = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=channels, dtype='int16')
        sd.wait() # Wait until recording is finished
        sf.write(filename, recording, samplerate)
        print(f"Recording saved to {filename}")
        return filename
    except Exception as e:
        print(f"Error during audio recording: {e}")
        return None

# --- Speech-to-Text Function ---
def speech_to_text(audio_file_path):
    """Transcribes audio file using Google Cloud Speech-to-Text."""
    # Requires GOOGLE_APPLICATION_CREDENTIALS environment variable to be set
    # to the path of your service account key file.
    try:
        client = speech.SpeechClient()
        with io.open(audio_file_path, "rb") as audio_file:
            content = audio_file.read()

        audio = speech.RecognitionAudio(content=content)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            language_code="en-US",
        )

        print("Transcribing audio...")
        response = client.recognize(config=config, audio=audio)

        if not response.results:
            print("Transcription failed: No speech detected or recognized.")
            return None

        transcript = "".join(result.alternatives[0].transcript for result in response.results)
        print(f"Transcription: {transcript}")
        return transcript

    except Exception as e:
        print(f"Error during speech-to-text: {e}")
        print("Ensure GOOGLE_APPLICATION_CREDENTIALS is set correctly and Speech-to-Text API is enabled.")
        return None

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
    if os.path.exists("./keys/token.json"):
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

    if not genai_file_parts:
        print("No files were successfully uploaded to GenAI. Aborting.")
        exit()
        # return client, None # Return client in case user wants to try generation without files?

    print(f"Using Gemini {model_name}")
    return client, genai_file_parts

def _get_next_chunk(iterator):
    """Helper to get next item from sync iterator, returning None on StopIteration."""
    try:
        return next(iterator)
    except StopIteration:
        return None

async def generate_response(client, file_parts, inquiry_text, model_name, conversation_history):
    """Generates a response, starting TTS early and maintaining conversation history."""
    print("\n📤 Inquiry\n")
    print(inquiry_text + "\n")

    last_usage_metadata = None
    response_text = ""
    first_chunk_text = ""
    remaining_text = ""
    first_chunk_spoken = False # We use this to know *when* to split text
    sentence_count = 0
    # Tasks for SYNTHESIS
    first_chunk_synthesis_task = None
    remaining_text_synthesis_task = None
    # Variables to hold synthesized AUDIO DATA
    first_chunk_audio = None
    remaining_text_audio = None
    # Task for first chunk PLAYBACK
    first_playback_task = None

    if not client:
        print("GenAI client is not available.")
        return None, None, conversation_history

    try:
        # Select model
        if model_name == 'flash':
            model_str = "models/gemini-1.5-flash-latest"
        elif model_name == 'flash-lite':
            model_str = "models/gemini-1.5-flash-latest" # Or a specific lite model if available
        elif model_name == 'pro':
             model_str = "models/gemini-1.5-pro-latest"
        else:
            model_str = "models/gemini-1.5-pro-latest" # Defaulting
            print(f"Warning: Invalid model '{model_name}' specified. Defaulting to {model_str}")

        # Construct initial prompt or use history
        if not conversation_history:
            try:
                with open("sys_prompts.json", "r") as f:
                    sys_prompts = json.load(f)
                if format == "chat":
                    system_instructions = sys_prompts["system_instruction_chat"]
                elif format == "voice":
                    system_instructions = sys_prompts["system_instruction_voice"]
                else:
                    system_instructions = sys_prompts["system_instruction_chat"] # Default
            except FileNotFoundError:
                print("Error: sys_prompts.json not found. Exiting.")
                exit()

            initial_user_parts = [types.Part.from_text(text=system_instructions)]
            if file_parts:
                initial_user_parts.extend(file_parts)
            initial_user_parts.append(types.Part.from_text(text="--- User Inquiry ---"))
            initial_user_parts.append(types.Part.from_text(text=inquiry_text))
            conversation_history.append(types.Content(role="user", parts=initial_user_parts))
            contents = conversation_history
        else:
            conversation_history.append(types.Content(role="user", parts=[types.Part.from_text(text=inquiry_text)]))
            contents = conversation_history

        generate_content_config = types.GenerateContentConfig(
            # temperature=1, # Consider adjusting if needed
            response_mime_type="text/plain",
        )

        # generate_content_stream seems to return a sync generator
        stream = client.models.generate_content_stream(
            model=model_str,
            contents=contents,
            config=generate_content_config,
        )

        print("\n🔊 Assistant:", end=" ")
        # Get the synchronous stream iterator
        sync_stream_iterator = iter(stream)

        while True:
            try:
                # Run the blocking next() call in a thread to avoid blocking asyncio loop
                # Use the helper function to catch StopIteration within the thread
                chunk = await asyncio.to_thread(_get_next_chunk, sync_stream_iterator)

                # Check if the iterator is exhausted
                if chunk is None:
                    break # Exit the loop if stream ended

                # Process the chunk (same logic as before)
                if chunk.text:
                    text_piece = chunk.text
                    print(text_piece, end="", flush=True)
                    response_text += text_piece # Accumulate full response

                    if not first_chunk_spoken:
                        first_chunk_text += text_piece
                        # Simple sentence counting based on terminal punctuation
                        sentence_count += text_piece.count('.') + text_piece.count('?') + text_piece.count('!')
                        if sentence_count >= SENTENCES_PER_CHUNK:
                            print(f"\n[Starting TTS synthesis task for first {sentence_count} sentence(s)...]")
                            # Start SYNTHESIS for the first chunk in the background
                            first_chunk_synthesis_task = asyncio.create_task(synthesize_text_async(first_chunk_text))
                            first_chunk_spoken = True
                            # No longer need first_chunk_text after task creation
                    else:
                        # Once the first chunk is being spoken, accumulate the rest
                        remaining_text += text_piece

                if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                    last_usage_metadata = chunk.usage_metadata

            except Exception as e:
                # Handle other potential errors during stream processing loop
                print(f"\nError during stream processing loop: {e}")
                # Optionally add more specific error handling or re-raise
                break

        print() # Newline after response stream finishes

        # --- Start synthesis for remaining text immediately after stream ends ---
        if remaining_text and not first_chunk_synthesis_task:
             # This case should ideally not happen if SENTENCES_PER_CHUNK > 0
             # but handles edge case where first chunk logic didn't trigger a task
             print("\n[Response shorter than chunk size, preparing to speak all at once...]")
             # We'll handle the actual await later
        elif remaining_text:
            print(f"\n[LLM stream finished. Starting TTS synthesis task for remaining {len(remaining_text.split())} words...]")
            remaining_text_synthesis_task = asyncio.create_task(synthesize_text_async(remaining_text))

        # --- Wait for Synthesis Tasks to Complete --- #
        # --- Wait for Synthesis Tasks (and start first playback) --- #
        print("\n[Waiting for TTS synthesis tasks to complete...]")
        if first_chunk_synthesis_task:
            first_chunk_audio = await first_chunk_synthesis_task
            # If first synthesis succeeded, start its playback in background
            if first_chunk_audio:
                print("\n[Starting playback task for first audio chunk...]")
                first_playback_task = asyncio.create_task(play_audio_chunk_async(first_chunk_audio))
            else:
                print("\n[Skipping playback for first chunk (synthesis failed or no audio)]")

        # Wait for the second synthesis task (might happen during first playback)
        if remaining_text_synthesis_task:
            remaining_text_audio = await remaining_text_synthesis_task

        # --- Handle Sequential PLAYBACK --- #
        # Ensure first playback is finished before starting second
        if first_playback_task:
            print("\n[Ensuring first chunk playback is finished...]")
            await first_playback_task

        # Now play the second chunk if available
        if first_chunk_synthesis_task:
            # If we started speaking the first chunk...
            # We already waited for first playback above.
            # Play the second chunk now.
            if remaining_text_audio:
                 print("\n[Playing remaining audio chunk...]")
                 await play_audio_chunk_async(remaining_text_audio)
        elif response_text:
            # If the response finished before reaching SENTENCES_PER_CHUNK
            print("\n[Response shorter than chunk size, synthesizing and speaking all at once...]")
            full_audio = await synthesize_text_async(response_text)
            if full_audio:
                 await play_audio_chunk_async(full_audio)
        else:
            # Handle case where LLM returned nothing
            print("\nWarning: LLM returned an empty response. No TTS.")

        # Update conversation history *after* processing the full response
        if response_text:
            conversation_history.append(types.Content(role="model", parts=[types.Part.from_text(text=response_text)]))

    except Exception as e:
        print(f"\nAn error occurred during generation or TTS handling: {e}")
        # Attempt to update history even on error, if we have some response text
        if response_text and not any(c.role == 'model' and c.parts[0].text == response_text for c in conversation_history):
             conversation_history.append(types.Content(role="model", parts=[types.Part.from_text(text=response_text)]))
        # Return None for text on error, but keep history
        return None, None, conversation_history

    # Return the full response text along with metadata and history
    return response_text, last_usage_metadata, conversation_history

async def main(): # Wrap main logic in an async function
    args = parse_arguments()
    model_name = args.model

    # Prepare files and client ONCE
    genai_client, prepared_file_parts = prepare_context_files(args.download)

    if not genai_client:
        print("Exiting due to issue with GenAI client initialization.")
        return # Use return instead of exit in async main

    # Initialize conversation history
    conversation_history = []

    # Main inquiry loop
    while True:
        inquiry_to_use = args.inquiry # Get inquiry from args for the *first* iteration
        if inquiry_to_use:
            args.inquiry = None # Clear it so we prompt next time
        else:
            try:
                # Use asyncio.to_thread for synchronous input in async context if needed
                # For simplicity here, assuming input() works okay or is acceptable block
                user_input = await asyncio.to_thread(input, "\nEnter text prompt, press Enter to record audio, or 'q' to quit: ")
                # user_input = input("\nEnter text prompt, press Enter to record audio, or 'q' to quit: ")
                if user_input.lower() == 'q':
                    print("Exiting.")
                    break
                elif not user_input: # User pressed Enter, initiate recording
                    # Run synchronous recording/STT in a thread executor
                    audio_path = await asyncio.to_thread(record_audio)
                    if audio_path:
                        inquiry_to_use = await asyncio.to_thread(speech_to_text, audio_path)
                        # Clean up the temporary audio file (sync os call is okay)
                        try:
                            os.remove(audio_path)
                        except OSError as e:
                            print(f"Warning: Could not remove temp audio file {audio_path}: {e}")
                    else:
                        inquiry_to_use = None # Recording failed
                else:
                    inquiry_to_use = user_input # Use text input

            except EOFError: # Handle Ctrl+D
                print("\nExiting.")
                break

        if not inquiry_to_use:
            print("No input received or transcription failed. Please try again or press 'q' to quit.")
            continue

        # Process this single inquiry, passing and updating history
        start_time = time.time()
        # Call the async generate_response function
        llm_response_text, final_usage_metadata, conversation_history = await generate_response(
            genai_client,
            prepared_file_parts,
            inquiry_to_use,
            args.model,
            conversation_history, # Pass current history
        )
        end_time = time.time()
        duration = end_time - start_time

        # TTS playback is now handled *inside* generate_response
        # Remove the separate TTS call block:
        # if llm_response_text:
        #      print("\n--- Starting TTS Playback ---")
        #      try:
        #          asyncio.run(speak_text_async(llm_response_text)) # REMOVED
        #      except Exception as e:
        #          print(f"\nError during TTS playback: {e}")
        #      print("--- Finished TTS Playback ---")
        # else:
        #      print("\nSkipping TTS playback as no response was generated.")

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


if __name__ == "__main__":
    args = parse_arguments()
    model_name = args.model
    try:
        asyncio.run(main()) # Run the async main function
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")
