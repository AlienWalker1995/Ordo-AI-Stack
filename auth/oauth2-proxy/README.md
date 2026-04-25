# oauth2-proxy

`emails.txt` is the Google-account allowlist for the SSO front door.
One email per line. Only listed emails can complete the OIDC dance.

This file is committed with a placeholder (`YOUR_ALLOWLIST_EMAIL`).
Replace it locally; do **not** commit your real email.

To reload after editing: `docker compose restart oauth2-proxy`.
