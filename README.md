# SkyeBrowse Customer Inquiry Responder

This script uses Google Gemini (via the `google-genai` library) and context documents stored in Google Drive to generate responses to customer support inquiries about SkyeBrowse. It can download the necessary training documents from Google Drive or use local copies if available.

## Setup

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd <repository-directory>
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
    *(The script will prompt you to enter the inquiry.)*

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
*   `.env`: Stores your `GEMINI_API_KEY`.
*   `drive/`: Directory where Google Drive context files are downloaded (if using the `-d` flag or if they exist locally).

*(These files/directories are listed in `.gitignore` to prevent accidental commits.)*
