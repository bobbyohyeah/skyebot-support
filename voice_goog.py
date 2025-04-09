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
import pygame
from pydub import AudioSegment
import httpx
import sounddevice as sd
import soundfile as sf
from google.cloud import speech
import re # Add import for regex
extension = "txt"
format = "voice" # voice or chat or email

load_dotenv(override=True)

# --- Audio Recording Settings ---
SAMPLE_RATE = 16000 # Sample rate for recording (Hz) - common for STT
CHANNELS = 1       # Mono audio
AUDIO_FILENAME = "temp_input_audio.wav"

# --- Initialize Pygame Mixer ---
try:
    pygame.mixer.init(frequency=24000, size=-16, channels=1)
    print("Pygame mixer initialized successfully.")
except pygame.error as e:
    print(f"Error initializing pygame mixer: {e}. Audio output will be disabled.")
    pygame = None # Disable pygame if initialization fails

# --- Text-to-Speech Function ---
def text_to_speech(text):
    """Convert text to audio using Google TTS service"""
    tts_api_key = os.environ.get("GOOGLE_TTS_API_KEY")
    if not tts_api_key:
        print("Warning: GOOGLE_TTS_API_KEY environment variable not set. Cannot perform text-to-speech.")
        return None
    if not pygame: # Check if pygame initialization failed
        print("Warning: Pygame not initialized. Skipping text-to-speech.")
        return None

    try:
        response = httpx.post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            headers={"X-Goog-Api-Key": tts_api_key},
            json={
                "input": {"text": text},
                "voice": {"languageCode": "en-US", "name": "en-US-Chirp3-HD-Leda"},
                "audioConfig": {
                    "audioEncoding": "LINEAR16",
                    "speakingRate": 1
                }
            },
            timeout=20.0
        )
        response.raise_for_status() # Raise an exception for bad status codes
        audio_content_base64 = response.json().get('audioContent')
        if audio_content_base64:
            import base64
            return base64.b64decode(audio_content_base64)
        else:
            print("Warning: No audio content received from TTS API.")
            return None
    except httpx.RequestError as exc:
        print(f"Error during TTS request: {exc}")
        return None
    except httpx.HTTPStatusError as exc:
        print(f"TTS API returned error status: {exc.response.status_code} - {exc.response.text}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred in text_to_speech: {e}")
        return None

# --- Audio Streaming Function ---
def stream_audio(audio_data, tts_channel):
    """Stream audio chunk using pygame by queueing on a channel."""
    if not audio_data or not pygame:
        return # Don't proceed if no audio data or pygame is disabled

    try:
        audio = AudioSegment(
            data=audio_data,
            sample_width=2, # 16 bits = 2 bytes
            frame_rate=24000,
            channels=1
        )
        # Apply a short fade-in to reduce popping
        fade_ms = 10 # milliseconds
        audio_faded = audio.fade_in(fade_ms)
        
        sound = pygame.mixer.Sound(buffer=audio_faded.raw_data)

        if tts_channel:
            # Queue the sound on the dedicated channel
            tts_channel.queue(sound)
            # Optional: Add a small buffer time maybe? 
            # No explicit wait needed here, channel handles playback.
        else:
            # Fallback if channel wasn't reserved (e.g., pygame init issue)
            print("Warning: TTS channel not available. Playing sound directly.")
            sound.play()
            # Fallback requires waiting if no channel queueing
            while pygame.mixer.get_busy(): 
                pygame.time.Clock().tick(10)

    except Exception as e:
        print(f"Error playing/queueing audio chunk: {e}")

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


def generate_response(client, file_parts, inquiry_text, model_name, conversation_history, tts_channel):
    """Generates a response, maintaining conversation history."""
    print("\n📤 Inquiry\n")
    print(inquiry_text + "\n")

    last_usage_metadata = None
    response_text = ""
    text_buffer = "" # Buffer for incoming text
    # Removed tts_buffer and sentence counter

    if not client:
        print("GenAI client is not available.")
        return None, conversation_history

    try:
        # Select model
        if model_name == 'flash':
            model_str = "models/gemini-1.5-flash-latest"
        elif model_name == 'flash-lite':
            model_str = "models/gemini-1.5-flash-latest" # Or a specific lite model if available
        elif model_name == 'pro':
             model_str = "models/gemini-1.5-pro-latest"
        else:
            model_str = "models/gemini-1.5-pro-latest"
            print(f"Warning: Invalid model '{model_name}' specified. Defaulting to {model_str}")

        # Construct initial prompt or use history
        if not conversation_history:
            try:
                with open("sys_prompts.json", "r") as f:
                    sys_prompts = json.load(f)
            except FileNotFoundError:
                print("Error: sys_prompts.json not found. Exiting.")
                exit()
            if format == "chat":
                initial_user_parts = [types.Part.from_text(text=sys_prompts["system_instruction_chat"])]
            elif format == "voice":
                initial_user_parts = [types.Part.from_text(text=sys_prompts["system_instruction_voice"])]
            else:
                initial_user_parts = [types.Part.from_text(text=sys_prompts["system_instruction_chat"])]
             
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
            temperature=1,
            response_mime_type="text/plain",
        )

        stream = client.models.generate_content_stream(
            model=model_str,
            contents=contents,
            config=generate_content_config,
        )

        print("\n🔊 Assistant:", end=" ")
        for chunk in stream:
            if chunk.text:
                print(chunk.text, end="", flush=True)
                response_text += chunk.text
                text_buffer += chunk.text

                # Process based on punctuation/length for streaming TTS
                search_start = 0
                while True:
                    match = re.search(r"[.?!](?=\s|$)", text_buffer[search_start:])
                    if match:
                        current_end_pos = search_start + match.end()
                        segment_to_process = text_buffer[:current_end_pos]
                        
                        # Heuristic check (e.g., > 15 chars or contains punctuation)
                        if len(segment_to_process) > 15 or re.search(r"[.?!]", segment_to_process):
                            audio_data = text_to_speech(segment_to_process.strip())
                            if audio_data:
                                stream_audio(audio_data, tts_channel) # Pass channel
                            
                            text_buffer = text_buffer[current_end_pos:]
                            search_start = 0
                        else:
                            # Segment too short, wait for more text
                            search_start = current_end_pos
                    else:
                        break # No more terminators

            if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
                last_usage_metadata = chunk.usage_metadata

        # Process any final fragment left in the buffer
        if text_buffer.strip():
            audio_data = text_to_speech(text_buffer.strip())
            if audio_data:
                stream_audio(audio_data, tts_channel) # Pass channel
        print() # Newline after response

        if response_text:
            conversation_history.append(types.Content(role="model", parts=[types.Part.from_text(text=response_text)]))

    except Exception as e:
        print(f"\nAn error occurred during generation: {e}")

    return last_usage_metadata, conversation_history

if __name__ == "__main__":
    args = parse_arguments()
    model_name = args.model

    # Initialize Pygame and reserve a channel for TTS playback
    tts_channel = None
    if pygame: # Check if pygame was initialized successfully
        try:
            # Allocate a specific channel (e.g., channel 0)
            tts_channel = pygame.mixer.Channel(0) 
            print("Reserved Pygame mixer channel 0 for TTS playback.")
        except pygame.error as e:
            print(f"Error getting pygame channel: {e}. Audio queueing might not work.")
            # Continue without a dedicated channel? Or handle differently?
            # For now, stream_audio will need to handle tts_channel being None

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
                user_input = input("\nEnter text prompt, press Enter to record audio, or 'q' to quit: ")
                if user_input.lower() == 'q':
                    print("Exiting.")
                    break
                elif not user_input: # User pressed Enter, initiate recording
                    audio_path = record_audio() # Record for default duration
                    if audio_path:
                        inquiry_to_use = speech_to_text(audio_path)
                        # Clean up the temporary audio file
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
        final_usage_metadata, conversation_history = generate_response(
            genai_client,
            prepared_file_parts,
            inquiry_to_use,
            args.model,
            conversation_history, # Pass current history
            tts_channel # Pass the reserved channel
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
