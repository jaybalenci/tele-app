"""DoorDash auth constants and notes.

Auth paths (do not mix):

1. Identity / risk-bff (signup, MFA)
   - Authorization: DOORDASH_AUTHORIZATION  (static, same every request)
   - XSRF-TOKEN cookie + X-XSRF-TOKEN header  (per session, from GET)
   - Domains: identity.doordash.com, risk-bff.doordash.com

2. Consumer mobile BFF (logged-in Android API)
   - Authorization: JWT <access_token>  (per account, from /api/v1/auth/token)
   - Domain: consumer-mobile-bff.doordash.com

3. Consumer web GraphQL (group cart, checkout in browser)
   - Session cookies (ddweb_token, ddweb_session_id, etc.) — not DOORDASH_AUTHORIZATION
   - csrf_token cookie + x-csrftoken header
   - Domain: www.doordash.com

Cloudflare: no cf_clearance injection or solver in this project. Rely on
curl_cffi impersonate + residential proxy; session jar picks up whatever CF sets.
"""

# Static Android OAuth client credential — identity/risk-bff only. Never rotate.
DOORDASH_AUTHORIZATION = "FtrOvqTNyAkAAAAAAAAAADpTSUJ1bUKhAAAAAAAAAACeT6l00rBlswAAAAAAAAAA"

IMPERSONATE = "chrome120"
