# oauth2-proxy

The Google-account allowlist for the SSO front door is the `site: SSO_ALLOWED_EMAILS` key in
`out/ordo.yaml` (comma-separated). Only listed emails can complete the OIDC dance.

`ordo remote enable` sets it; edit the key and run `ordo apply` to change it. The render writes
`out/oauth2-proxy/emails.txt` (one email per line), which oauth2-proxy mounts read-only and
reloads on change. Nothing here is committed with a real address.
