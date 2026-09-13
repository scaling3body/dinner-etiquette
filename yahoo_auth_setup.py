"""
yahoo_auth_setup.py

RUN THIS LOCALLY ON YOUR OWN COMPUTER ONCE (not in GitHub Actions -- it needs
a real browser and interactive terminal).

Before running:
1. Go to https://developer.yahoo.com/apps/create/ and create an app.
   - Redirect URI: https://localhost:8080 (or whatever the current Yahoo
     developer console suggests -- oob/"installed application" style is fine)
   - API Permissions: check "Fantasy Sports" with Read/Write access.
2. Note your Client ID (consumer key) and Client Secret (consumer secret).
3. pip install yahoo_oauth
4. Run: python yahoo_auth_setup.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET
5. Follow the printed URL, log into Yahoo, approve access, and paste the
   verification code back into the terminal when prompted.

This produces oauth2.json in this folder. Its contents (the whole file) get
pasted into a GitHub secret called YAHOO_OAUTH_JSON -- see README.md.
Do NOT commit oauth2.json to your repo.
"""

import sys
import json
from yahoo_oauth import OAuth2


def main():
    if len(sys.argv) != 3:
        print("Usage: python yahoo_auth_setup.py <consumer_key> <consumer_secret>")
        sys.exit(1)

    consumer_key, consumer_secret = sys.argv[1], sys.argv[2]

    creds = {"consumer_key": consumer_key, "consumer_secret": consumer_secret}
    with open("oauth2.json", "w") as f:
        json.dump(creds, f)

    # This will walk you through the interactive browser-based OAuth flow
    # and rewrite oauth2.json with access_token / refresh_token included.
    sc = OAuth2(None, None, from_file="oauth2.json")
    print("\nAuthenticated! oauth2.json now contains your tokens.")
    print("Paste the FULL CONTENTS of oauth2.json into a GitHub secret named YAHOO_OAUTH_JSON.")


if __name__ == "__main__":
    main()
