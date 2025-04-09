#!/usr/bin/env python3

"""Simple example to generate audio with preset voice using async/await and stream playback with buffering"""

import asyncio
import edge_tts
import os
import tempfile

# TEXT = "I am having trouble downloading your app to my Mavic 3 Enterprise. Is there some trick or am I missing something???"
TEXT = " I like the platform you have provided. Video to 3 D Modeling. I see you hold the information on your cloud with a link to view. I have a need to make a 3d model and share it with my clients. Is there a way to pay for the model after its rendered to give it to my clients as they see fit for advertising, sharing, or hold it on their on main frame storage?"
VOICE = "en-US-AvaNeural"
SENTENCES_PER_CHUNK = 3 # Target number of sentences per playback chunk

async def play_audio_chunk_async(audio_data):
    """Helper coroutine to play a chunk of audio data using afplay."""
    if not audio_data:
        print("Warning: No audio data received to play.")
        return

    temp_file_path = None # Ensure it's defined for the finally block
    try:
        # Create a temporary file to store the audio chunk
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_audio_file:
            temp_audio_file.write(audio_data)
            temp_file_path = temp_audio_file.name

        # Play the temporary file using afplay
        process = await asyncio.create_subprocess_exec(
            "afplay",
            temp_file_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, # Capture stderr for potential errors
        )
        stdout, stderr = await process.communicate() # Wait for completion and get output

        if process.returncode != 0:
            error_message = stderr.decode().strip() if stderr else 'Unknown error'
            print(f"\n[afplay error: {error_message}]")

    except Exception as e:
        print(f"\n[Error during audio playback: {e}]")
    finally:
        # Clean up the temporary file even if errors occurred
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError as e:
                print(f"\n[Warning: Could not remove temp file {temp_file_path}: {e}]")

async def amain() -> None:
    """Main function"""
    communicate = edge_tts.Communicate(TEXT, VOICE)
    print("Streaming audio (buffered)...")

    audio_buffer = bytearray()
    text_buffer = "" # To accumulate text for sentence detection
    sentence_count = 0
    # More robust sentence end detection could use regex, but simple check is often okay
    sentence_ends = {'.', '?', '!'}

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_buffer.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            word_text = chunk["text"]
            text_buffer += word_text + " " # Add word and space to text buffer

            # Simple check if the word ends a sentence
            if word_text and word_text[-1] in sentence_ends:
                sentence_count += 1

            # Check if we have enough sentences and corresponding audio data
            if sentence_count >= SENTENCES_PER_CHUNK and audio_buffer:
                print(f"\n[Playing chunk - {sentence_count} sentence(s)]")
                # print(f"Text: '{text_buffer.strip()}'") # Debug: print text chunk
                await play_audio_chunk_async(bytes(audio_buffer)) # Play the collected audio

                # Reset buffers and counter for the next chunk
                audio_buffer.clear()
                text_buffer = ""
                sentence_count = 0

    # Play any remaining audio left in the buffer after the loop finishes
    if audio_buffer:
        print(f"\n[Playing remaining chunk]")
        # print(f"Text: '{text_buffer.strip()}'") # Debug: print final text chunk
        await play_audio_chunk_async(bytes(audio_buffer))

    print("\nAudio streaming finished.")

if __name__ == "__main__":
    asyncio.run(amain())