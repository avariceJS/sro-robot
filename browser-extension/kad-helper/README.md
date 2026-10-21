# SRO Robot KAD helper

Manifest V3 extension for the employee's ordinary browser. It searches a case in
KAD, displays visible chronology titles and dates, requires an explicit employee
choice, then transfers the selected PDF and source metadata to the authenticated
office service for server-side OCR and review.

## Store builds

The release builder is `app/kad_helper_package.py`; it has no dependency on app
settings, environment files, databases, credentials, or browser profiles.

```sh
python3 scripts/dev/package-kad-helper-stores.py --output-dir /tmp/kad-store-candidates
```

The default command builds the Chrome and Edge ZIPs, pinned to
`https://sro-office.ru` and `https://kad.arbitr.ru`. These candidates contain
no loopback permissions or per-request origin rewriting. Chrome and Edge use
the Chromium service worker package and required `debugger` permission.
Firefox remains controlled legacy/developer support and is not ready for AMO
submission; `--include-firefox-experimental` builds its separate experimental
archive explicitly. Store links appear to staff only after the official listing
URL is configured in production.

## Controlled development install

The source manifest and authenticated debug-only ZIP retain the localhost
development service. Never distribute the source or unpacked development build
to staff. Production `/kad-helper/download` is disabled; production installs come
from the approved official browser listing.

## Boundaries

- The extension interacts only with the approved office pages and its owned KAD
  tab. It does not extract, copy, or export cookies or credentials; the
  authenticated office page handles admission. It does not use direct search
  APIs or an unattended browser.
- Visible chronology may be incomplete. A document title or date does not
  classify its legal type.
- KAD access checks, challenges, changed candidates, and unknown receipt outcomes
  stop or require reconciliation. The extension never bypasses these controls.
- PDF and source metadata are sent to the authenticated office service. OCR and
  text parsing run on that server; the helper does not save a record to SROArbitr.
- Chrome and Edge display their browser-owned debugger notice while transferring
  the selected PDF. Firefox requires its optional response-reading permission
  and is not a publication-ready store target.

See `docs/KAD_STORE_PUBLICATION.md`, `docs/KAD_HELPER_ADMIN.md`, and the public
`/kad-helper/privacy` page for store review, permission disclosures, managed
deployment, and data handling.
