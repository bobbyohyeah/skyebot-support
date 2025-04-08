import asyncio
import os
import sounddevice as sd
from google import genai
from google.generativeai import types

# --- Configuration ---
# IMPORTANT: Set your API Key as an environment variable
# export GEMINI_API_KEY="YOUR_API_KEY"
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable not set.")

MODEL_NAME = "gemini-2.0-flash-exp" # Or another model supporting the Live API like gemini-2.0-flash-exp
AUDIO_SAMPLE_RATE = 24000 # Output sample rate from Gemini Live API
AUDIO_CHANNELS = 1
AUDIO_DTYPE = 'int16' # Output audio format is 16-bit PCM

# --- Gemini Client Setup ---
client = genai.Client(api_key=API_KEY) # Default uses v1beta, no need for http_options for 1.5 Flash

# --- Live API Configuration ---
live_config = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    # Optional: Uncomment to change the voice
    # speech_config=types.SpeechConfig(
    #     voice_config=types.VoiceConfig(
    #         prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
    #     )
    # )
)

async def main():
    print(f"Connecting to model: {MODEL_NAME}")
    print("Enter text to speak, or type 'exit' to quit.")

    # --- Setup Audio Output Stream ---
    audio_queue = asyncio.Queue()

    def audio_callback(outdata, frames, time, status):
        if status:
            print(status, flush=True)
        try:
            data = audio_queue.get_nowait()
            if len(data) < len(outdata):
                outdata[:len(data)] = data
                outdata[len(data):] = b'\x00' * (len(outdata) - len(data)) # Pad with silence
                raise sd.CallbackStop # Stop if queue runs out mid-callback
            else:
                outdata[:] = data
        except asyncio.QueueEmpty:
            outdata[:] = b'\x00' * len(outdata) # Fill with silence if queue is empty
            raise sd.CallbackStop

    # --- Connect to Gemini Live API ---
    try:
        async with client.aio.live.connect(model=MODEL_NAME, config=live_config) as session:
            print("Connected. Ready for input.")

            # --- Audio Playback Task ---
            loop = asyncio.get_running_loop()
            stream_event = asyncio.Event()

            def start_stream():
                stream = sd.RawOutputStream(
                    samplerate=AUDIO_SAMPLE_RATE,
                    channels=AUDIO_CHANNELS,
                    dtype=AUDIO_DTYPE,
                    callback=audio_callback,
                    finished_callback=stream_event.set # Signal when stream finishes
                )
                stream.start()
                return stream

            current_stream = None

            # --- Main Interaction Loop ---
            while True:
                try:
                    user_input = await loop.run_in_executor(None, input, "User> ")
                except EOFError:
                    print("\nExiting.")
                    break

                if not user_input:
                    continue
                if user_input.lower() == "exit":
                    print("Exiting.")
                    break

                # --- Send Text to API ---
                await session.send(input=user_input, end_of_turn=True)
                print("Gemini> ", end="", flush=True)

                # --- Receive and Queue Audio ---
                audio_data_received = False
                async for response in session.receive():
                    if response.data:
                        if not audio_data_received:
                            # Start new stream only when first audio chunk arrives
                            if current_stream: # Ensure previous stream is closed if any
                                stream_event.set() # Ensure callback stops trying to get data
                                current_stream.close()
                            stream_event.clear()
                            current_stream = start_stream()
                            audio_data_received = True
                        await audio_queue.put(response.data)
                    # Optional: Print text response if needed (though modality is AUDIO)
                    # if response.text:
                    #     print(response.text, end="")

                # Wait for the current audio stream to finish playing
                if audio_data_received and current_stream:
                    await audio_queue.join() # Wait for queue to be processed
                    await stream_event.wait() # Wait for stream callback to signal finish
                    current_stream.close()
                    current_stream = None
                print() # Newline after Gemini speaks

    except Exception as e:
        print(f"\nAn error occurred: {e}")
    finally:
        if current_stream and not current_stream.closed:
            current_stream.abort() # Stop audio immediately on exit/error
            current_stream.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.") 