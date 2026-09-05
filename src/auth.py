import os
import logging
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
TOKEN_FILE = os.getenv('TOKEN_FILE_PATH', '.gcp-saved-tokens.json')
SCOPES = [os.getenv('CALENDAR_SCOPES', 'https://www.googleapis.com/auth/calendar')]
REDIRECT_PORT = int(os.getenv('OAUTH_CALLBACK_PORT', 8080))
REDIRECT_URI = f'http://localhost:{REDIRECT_PORT}/oauth2callback'
# Note: REDIRECT_URI must be registered in your Google Cloud Console OAuth Client settings!

def get_credentials():
    """Gets valid Google API credentials. Handles loading, refreshing, and the OAuth flow."""
    creds = None

    # Check if mandatory config is present
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        logger.error("Missing GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET in .env file.")
        raise ValueError("Missing Google OAuth credentials in configuration.")

    # --- 1. Load existing tokens ---
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            logger.info("Loaded credentials from token file.")
        except Exception as e:
            logger.warning(f"Failed to load credentials from {TOKEN_FILE}: {e}. Will attempt re-authentication.")
            creds = None # Ensure creds is None if loading failed

    # --- 2. Refresh or Initiate Flow ---
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Credentials expired. Refreshing...")
            try:
                creds.refresh(Request())
                logger.info("Credentials refreshed successfully.")
            except Exception as e:
                logger.error(f"Failed to refresh credentials: {e}. Need to re-authenticate.")
                creds = None # Force re-authentication
        else:
            logger.info("No valid credentials found or refresh failed. Starting OAuth flow...")
            # Use client_secret dict directly for Flow
            client_config = {
                "installed": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost", REDIRECT_URI] # Add both for flexibility
                }
            }
            try:
                # Use InstalledAppFlow instead of Flow
                logger.info("Attempting authentication using InstalledAppFlow...")
                flow_installed = InstalledAppFlow.from_client_config(
                    client_config=client_config,
                    scopes=SCOPES,
                    redirect_uri=REDIRECT_URI # Ensure this matches console setup
                )
                # This method should handle the server start, browser opening, and code retrieval.
                creds = flow_installed.run_local_server(
                    port=REDIRECT_PORT,
                    authorization_prompt_message="Please visit this URL to authorize:\n{url}",
                    success_message="Authentication successful! You can close this window.",
                    open_browser=True
                )
                logger.info("InstalledAppFlow completed.")

            except Exception as e:
                logger.error(f"Error during InstalledAppFlow execution: {e}", exc_info=True)
                creds = None # Ensure creds is None on error

            if creds:
                # Save the credentials for the next run
                try:
                    with open(TOKEN_FILE, 'w') as token_file:
                        token_file.write(creds.to_json())
                    logger.info(f"Credentials saved successfully to {TOKEN_FILE}")
                except Exception as e:
                    logger.error(f"Failed to save credentials to {TOKEN_FILE}: {e}")
            else:
                logger.error("OAuth flow using InstalledAppFlow did not result in valid credentials.")
                return None

    # --- 3. Final Check ---
    if not creds or not creds.valid:
        logger.error("Failed to obtain valid credentials after all steps.")
        return None

    logger.info("Successfully obtained valid credentials.")
    return creds

# Example usage (can be called from server.py)
if __name__ == '__main__':
    print("Attempting to get Google Calendar credentials...")
    credentials = get_credentials()
    if credentials:
        print("Successfully obtained credentials.")
        print(f"Token URI: {credentials.token_uri}")
        # You can now use these credentials to build the service client
    else:
        print("Failed to obtain credentials.") 