**Overview:**

This script automates customer support email responses for SkyeBrowse using Google Gemini and context documents from Google Drive.

1.  **Input:** Takes a customer inquiry via command-line argument (`-i`), or prompts interactively if not provided. Additional flags:
    *   `-d`: Forces download of files from Google Drive and cleans the local `drive/` directory before downloading.
    *   `-m`: Specifies the GenAI model (`flash`, `flash-lite`, or `pro`), defaulting to `flash`.
    *   Typing 'q' quits the interactive prompt.
2.  **Context Preparation:**
    *   Fetches Google Drive file IDs from environment variables (prefixed with `GDRIVE_`).
    *   Determines if a download is necessary based on the `-d` flag or if the `drive/` directory is missing or empty.
    *   If `-d` was specified, it attempts to delete the existing `drive/` directory.
    *   Ensures the `drive/` directory exists.
    *   If downloading: Authenticates with the Google Drive API (`credentials.json`, `token.json`), downloads specified files, exports Google Docs as `text/plain` (saved as `.md`), Google Sheets as `text/csv` (saved as `.csv`), and downloads plain text files directly. Unsupported types are skipped. Files are saved in the local `drive/` directory.
    *   If not downloading: Uses existing `.md` and `.csv` files found in the `drive/` directory based on the environment variable names.
    *   Initializes the Google GenAI client using the `GEMINI_API_KEY` from the `.env` file.
    *   Uploads the prepared local files sequentially (text/markdown first, then CSV) to the Google GenAI API, determining the MIME type based on the file extension.
3.  **Response Generation:**
    *   Selects the appropriate Gemini model string based on the `-m` argument (`models/gemini-2.0-flash-001`, `models/gemini-2.0-flash-lite-001`, or `models/gemini-2.5-pro-exp-03-25`).
    *   Constructs a detailed prompt including a system instruction (defining persona, source adherence, drone check procedure, formatting rules, etc.), the uploaded context file parts, and the user's inquiry.
    *   Sends the request to the chosen Gemini model API and streams the response back.
4.  **Output:**
    *   Prints the generated email response to the console in real-time as it streams.
    *   After generation, displays a summary including the time taken and token usage (prompt, candidates, total) obtained from the API metadata.
5.  **Looping:** Continues to prompt for new inquiries (handling empty input and EOFError) until the user quits ('q'). 