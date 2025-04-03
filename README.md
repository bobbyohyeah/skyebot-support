# SkyeBot Support

**Overview:**

This script automates customer support email responses for SkyeBrowse using Google Gemini and context documents from Google Drive.

1.  **Input:** Takes a customer inquiry either via a command-line argument (`-i`) or through an interactive prompt if no argument is given. Typing 'q' quits the interactive prompt.
2.  **Context Preparation:**
    *   Fetches Google Drive file IDs from environment variables (prefixed with `GDRIVE_`).
    *   If the `-d` flag is used or local files are missing, it authenticates with the Google Drive API (using `credentials.json` and `token.json`), downloads specified files, and exports Google Docs/Sheets to text/CSV format respectively, saving them in a local `drive/` directory.
    *   If the `-d` flag is *not* used and local files exist in `drive/`, it uses those files.
    *   Uploads the prepared local files (downloaded or pre-existing) to the Google GenAI API for context.
3.  **Response Generation:**
    *   Initializes the Google GenAI client using an API key from the `.env` file.
    *   Constructs a prompt for the Gemini model (`gemini-2.0-flash` specified in the code), including a system instruction (defining the persona as a SkyeBrowse support specialist) and the user's inquiry, along with the uploaded context files.
    *   Sends the request to the Gemini API and streams the response back.
4.  **Output:**
    *   Prints the generated email response to the console in real-time as it streams.
    *   After generation, displays a summary including the time taken and token usage (prompt, candidates, total) if available from the API metadata.
5.  **Looping:** Continues to prompt for new inquiries until the user quits.


## Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/bobbyohyeah/skyebot-support
    cd skyebot-support
    ```
2.  **Install dependencies (tested on python 3.11):**
    ```bash
    python -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```
3.  **Google Drive Credentials:**
    *   Enable the Google Drive API in your Google Cloud Console.
    *   Create OAuth 2.0 Client ID credentials (Desktop app type).
    *   Download the credentials JSON file and save it as `credentials.json` in the project's root directory.
4.  **Gemini API Key:**
    *   Obtain an API key for Google Gemini (e.g., from Google AI Studio).
    *   Create a file named `.env` in the project's root directory.
    *   Add your API key to the `.env` file:
        ```
        GEMINI_API_KEY=YOUR_API_KEY_HERE
        ```
5.  **First Run & Authentication:**
    *   When you run the script for the first time (`python main.py`), it will attempt to authenticate with Google Drive.
    *   Your web browser should open, prompting you to authorize the application.
    *   Upon successful authorization, a `token.json` file will be created in the root directory. This file stores your access tokens.

## Usage

**Options:**

*   `-i`, `--inquiry "Your customer inquiry text"`: Provide the customer inquiry directly via the command line. If omitted, the script will prompt you interactively.
*   `-d`, `--download`: Force the script to download/update files from Google Drive. If omitted, the script will look for existing files in the `drive/` directory first.

**Examples:**

*   **Run interactively, using local files if available:**
    ```bash
    python main.py
    ```
    *(You will be prompted to enter the inquiry.)*

*   **Run interactively, forcing download from Google Drive:**
    ```bash
    python main.py -d
    ```

*   **Provide inquiry directly, using local files if available:**
    ```bash
    python main.py -i "How do I reset my password?"
    ```

*   **Provide inquiry directly, forcing download from Google Drive:**
    ```bash
    python main.py -d -i "What are the system requirements for SkyeBrowse?"
    ```

**Exiting:** Type `q` and press Enter at the interactive prompt to quit.

## Dependencies

The required Python packages are listed in `requirements.txt`. Key dependencies include:

*   `google-api-python-client`
*   `google-auth-oauthlib`
*   `google-genai`
*   `python-dotenv`

*(See `requirements.txt` for the complete list of dependencies and specific versions.)*

## Important Files (Not Committed)

*   `credentials.json`: Your downloaded Google Cloud OAuth 2.0 credentials. Required for Google Drive API access.
*   `token.json`: Stores Google Drive API access and refresh tokens after successful authorization. Automatically generated/updated.
*   `.env`: Stores your `GEMINI_API_KEY` and other files to download from Google Drive. The Google Drive files are the url of it, so the file at `https://docs.google.com/document/d/1yRAq8aqxiOcQGkZ6hHv-xdZffZ2KPIGihmekzjKsyA0/edit?tab=t.0` would be reflected as `1yRAq8aqxiOcQGkZ6hHv-xdZffZ2KPIGihmekzjKsyA0`.
*   `drive/`: Directory where Google Drive context files are downloaded (if using the `-d` flag or if they exist locally).
