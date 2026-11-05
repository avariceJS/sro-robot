# KAD helper store publication

## State

As recorded in the 2026-11-05 owner handoff, Chrome's unlisted `0.3.0` item is
published and the production helper page links to its official listing. Confirm
the current listing and production configuration from the active release record
before making changes. Store publication, production configuration and a Windows
pilot are separate outcomes.

The Chrome and Edge packages are built from `store_archive("chrome")` and
`store_archive("edge")` in `app/kad_helper_package.py`. Each pins the service to
`https://sro-office.ru` and the source to `https://kad.arbitr.ru`; the builder
excludes the developer README and local/loopback origins. Do not distribute a
developer ZIP or unpacked build. Firefox remains a separate development target
and requires its own manifest, disclosure and qualification before store
submission.

From the repository root, a publisher can create the review ZIP in a temporary
directory after the candidate has been reviewed:

```sh
python3 -c 'from pathlib import Path; from app.kad_helper_package import store_archive; Path("/tmp/sro-kad-helper-chrome.zip").write_bytes(store_archive("chrome"))'
python3 -c 'from pathlib import Path; from app.kad_helper_package import store_archive; Path("/tmp/sro-kad-helper-edge.zip").write_bytes(store_archive("edge"))'
```

Upload only the corresponding package to the matching publisher portal. These
commands package source; they do not publish, sign off, or authorize deployment.
Do not add private profiles, credentials, office documents, or local test data.

## Product and data disclosures

Store listing text must describe the actual workflow. The helper searches a
case in the employee's ordinary KAD browser tab, displays the visible act titles
and chronology dates, and waits for the employee to choose a PDF, including when
only one candidate is visible. It transfers only that selected document to the
authenticated office service over HTTPS. The office service stores it for source
receipt and runs its office-hosted OCR/text parser. Employees review the extracted
fields and source before proceeding. The visible chronology may be incomplete;
the helper makes that limitation visible and still requires an employee choice,
even when one candidate is shown. A missing or changed candidate, access
challenge, acquisition failure, mismatch, or unknown receipt stops or requires
reconciliation. A title or date does not classify an act.

The extension does not access browser cookies or passwords, or read local/session
storage belonging to KAD. The office service page's own `sessionStorage` retains
only a pending request UUID and small resume flags; it never stores PDF bytes.
To bind the selected document to the browser response, each browser variant
observes request URL and method plus response status and MIME metadata for the
owned selected-document response chain. The chain can include bounded transient
HTML used for handoff and challenge checks. Only the accepted PDF and its
candidate/source provenance are sent to the office service; transient HTML is
not exported. The helper does not read or replay credential headers, cookies, or
request bodies, and does not use a native host, localhost service, cloud OCR, or
cloud AI in the store package. The PDF goes to the authenticated office HTTPS
service using the employee's current sign-in. Chrome and Edge show their
unavoidable browser debugging notice while the response chain is inspected;
cancelling it stops the transfer. The extension does not save or submit a record
to SROArbitr.

| Store package | Declared permissions | Host access |
| --- | --- | --- |
| Chrome / Edge | `storage`, `debugger` | `https://kad.arbitr.ru/*`, `https://sro-office.ru/*` |
| Firefox | `storage`; optional `webRequest`, `webRequestBlocking`, `webRequestFilterResponse` | `https://kad.arbitr.ru/*`, `https://sro-office.ru/*` |

There is no `cookies`, `tabs`, or `nativeMessaging` permission. Chrome 121+ does
not grant `debugger` as an optional permission, so Chrome/Edge declare it at
installation. Firefox's optional response-filter permission is not needed for
the Chrome/Edge package. Store declarations must match the uploaded package
variant and the publisher's current questionnaire.

The publisher must keep the listing, privacy notice and permission declarations
consistent with the exact package and production behavior. The privacy notice
must accurately explain collection, use, storage and sharing of case identifiers,
selected PDFs and task provenance. Do not claim that data never leaves the
browser: the selected PDF is sent to the office service.

## Store assets and rollout

- Extension icons: `browser-extension/kad-helper/icons/icon-16.png`, `icon-32.png`, `icon-48.png`, and `icon-128.png`.
- Chrome small promotional tile: `browser-extension/kad-helper/store-assets/chrome-small-promo-440x280.png`.
- Reproducible AppKit renderer: `browser-extension/kad-helper/store-assets/generate_assets.swift` (run with `swift browser-extension/kad-helper/store-assets/generate_assets.swift browser-extension/kad-helper/store-assets`).
- Store screenshots are maintained at `browser-extension/kad-helper/store-assets/chrome-candidate-selection-1280x800.png` and `chrome-helper-connection-1280x800.png`. They are listing assets, separate from the extension ZIP.
- Verify store listing, privacy URL, support contact, package hash, managed installation and a bounded synthetic Windows Chrome/Edge pilot against current publisher and release records before widening distribution.
- Never provide reviewers staff accounts, SROArbitr credentials, real case identifiers or real documents. Use a purpose-built synthetic reviewer account when access is needed.

To configure official listing links, use approved HTTPS URLs:

```text
APP_DEBUG=0
APP_PUBLIC_ORIGIN=https://sro-office.ru
KAD_HELPER_CHROME_STORE_URL=<approved Chrome Web Store listing URL>
KAD_HELPER_EDGE_STORE_URL=<approved Microsoft Edge Add-ons listing URL>
KAD_HELPER_FIREFOX_STORE_URL=<leave unset until separate Firefox AMO qualification>
```

Only approved official HTTPS listing URLs pass configuration validation. This
configuration controls links on the authenticated helper page; it does not
publish a package. Production changes still follow the release workflow. Do not
configure a Firefox listing before separate qualification.

## Official publisher references

- [Register a Chrome Web Store developer account](https://developer.chrome.com/docs/webstore/register)
- [Chrome Web Store image requirements](https://developer.chrome.com/docs/webstore/images)
- [Chrome Web Store privacy policy requirements](https://developer.chrome.com/docs/webstore/program-policies/privacy)
- [Register as an Edge extension developer](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/create-dev-account)
- [Publish an Edge extension](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/publish-extension)
- [Firefox Add-ons distribution](https://extensionworkshop.com/documentation/publish/distribute/)
- [Firefox Manifest V3 add-on ID requirement](https://extensionworkshop.com/documentation/develop/extensions-and-the-add-on-id/)
- [Firefox built-in data collection consent](https://extensionworkshop.com/documentation/develop/firefox-builtin-data-consent/)

Live KAD availability and selector coverage remain distinct from synthetic
browser fixture checks. Never bypass access challenges, infer legal document
type from its title/date, retry an uncertain receipt before reconciling it, or
use the helper to save or submit an SROArbitr record.
