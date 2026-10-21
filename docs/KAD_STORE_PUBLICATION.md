# KAD helper store publication

## State

The source candidate is version `0.3.0` and targets Chrome Web Store and
Microsoft Edge Add-ons. On 2026-10-21 the owner confirmed Chrome publisher
registration/payment and explicitly authorized submitting the current extension
through the authenticated Safari developer console. The verified 51,981-byte
Chrome ZIP (SHA-256 `c1c5fc203e59c32b49b4704a2890e87a551ac2cda323fd83fd129c8fb60d688c`)
was uploaded, but Google refused item creation because the publisher account
must enable two-step verification. No item ID, draft, submission, review or
public listing exists from this attempt. Account security setup is an owner
handoff; repeat the same verified upload only after the owner completes it.

The public privacy URL `https://sro-office.ru/kad-helper/privacy` still returned
HTTP 404 during this attempt. Do not declare that URL live or submit a placeholder.
An accessible privacy policy and synthetic reviewer access remain publication
prerequisites. The Windows Chrome/Edge live KAD pilot has not been run. Chrome
store authorization does not authorize a production website release or Edge
submission.

Firefox is a controlled developer/legacy experiment, not an AMO submission-ready
store target. The current Manifest V3 Firefox package lacks the required
`browser_specific_settings.gecko.id` and `gecko.data_collection_permissions`
declarations. A later AMO track needs a Firefox-specific manifest/data disclosure,
consent behavior compatible with the declared Firefox minimum version, and
separate Firefox qualification before anyone submits it. The current minimum is
Firefox 128, below the built-in consent support introduced in Firefox 140; a
submission must either raise the minimum or implement the required older-version
consent experience.

The Chrome/Edge store package is built from `store_archive("chrome")` or
`store_archive("edge")` in `app/kad_helper_package.py`; these are intentionally
separate package variants. Each statically pins the service to
`https://sro-office.ru` and the source to `https://kad.arbitr.ru`. The builder
excludes the developer README and local/loopback origins. Do not hand staff a
developer ZIP or unpacked build. Staff distribution is through an approved
official store listing; the authenticated `/kad-helper` page will link to that
listing only after its exact official URL is configured.

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

## Listing material

The following Russian copy is draft store text, not a publication claim.

**Name**

> СРО Робот — помощник КАД

**Short description**

> Поиск дела и передача выбранного сотрудником PDF в СРО Робот для локальной проверки.

**Full description**

> Помощник открывает поиск дела в обычной вкладке браузера и показывает видимые документы дела с названиями и датами хронологии. Сотрудник сам выбирает документ для передачи, даже если найден один вариант. Выбранный PDF отправляется в СРО Робот по защищённому соединению и обрабатывается на офисном сервере. Полученные поля и источник доступны сотруднику для проверки.
>
> Помощник не определяет юридический тип документа, не выбирает документ автоматически, не обходит проверки доступа и не сохраняет записи в СРО Арбитр. Если список документов неполон или результат передачи неизвестен, запуск останавливается для проверки.

**Search terms**

> КАД, картотека арбитражных дел, СРО Робот, судебные документы

The publisher must review this copy against the final privacy notice and the
exact production behavior before submission. Chrome and Edge require their own
publisher entries, privacy/data-use declarations, permission justifications,
language metadata, screenshots, support contact, and privacy-policy URL. A
privacy policy must accurately explain the extension's collection, use, storage,
and sharing of case identifiers, selected PDFs, and resulting task provenance.
Do not claim that data never leaves the browser: the selected PDF is sent to the
office service.

## Artifacts and open publication work

- Extension icons: `browser-extension/kad-helper/icons/icon-16.png`, `icon-32.png`, `icon-48.png`, and `icon-128.png`.
- Chrome small promotional tile: `browser-extension/kad-helper/store-assets/chrome-small-promo-440x280.png`.
- Reproducible AppKit renderer: `browser-extension/kad-helper/store-assets/generate_assets.swift` (run with `swift browser-extension/kad-helper/store-assets/generate_assets.swift browser-extension/kad-helper/store-assets`).
- Actual-interface screenshots at 1280×800: `browser-extension/kad-helper/store-assets/chrome-candidate-selection-1280x800.png` shows both candidate titles, dates, and selection controls; `browser-extension/kad-helper/store-assets/chrome-helper-connection-1280x800.png` shows setup and the connected state. These listing images are separate from the extension ZIP; only icons are packaged.
- Both were captured from headed Chromium 153.0.8010.12 on the current Mac, running the unpacked 0.3.0 Chrome extension against a local synthetic KAD fixture. They are genuine UI captures, not live KAD or store review evidence; no candidate PDF was selected in the showcase.
- Screenshot SHA-256: candidate selection `f34cdc14966c9f1a6236984d0b79a110875787fac34616f39a512f8695f9a0f8`; helper connection `c3bdf7ab5a78c86a541653625f94813e63ab0aef76df038144bcf24a8e00c092`.
- Windows Chrome/Edge store installation and live KAD pilot remain untested. The helper is not published; continue to use synthetic data for qualification.
- Large promotional artwork and localized screenshot variants are optional and have not been prepared.

Before a listing can be submitted, the owner/publisher must supply the legal
publisher identity and region, account owner and required approver, monitored
developer/support contact, an accurate public privacy-policy URL, and the final
listing text and assets. If a store reviewer needs access to the authenticated
office service, provide a purpose-built synthetic reviewer account and concise
test instructions. Never give reviewers staff accounts, SROArbitr credentials,
real case identifiers, or real documents. Chrome requires a one-time developer registration fee;
the official page states that a fee applies but the numeric amount is not recorded
here, so check the current dashboard before registering. Microsoft Edge extension
developer registration has no fee according to Microsoft Learn. The publisher
must complete each portal's identity checks, permission/data declarations, and
moderation process. No listing IDs or URLs are available yet.

After the store review approves the exact item, record its official listing URL
and store-assigned extension ID here and in the controlled release record. Then
set the appropriate production configuration:

```text
APP_DEBUG=0
APP_PUBLIC_ORIGIN=https://sro-office.ru
KAD_HELPER_CHROME_STORE_URL=<approved Chrome Web Store listing URL>
KAD_HELPER_EDGE_STORE_URL=<approved Microsoft Edge Add-ons listing URL>
KAD_HELPER_FIREFOX_STORE_URL=<leave unset until separate Firefox AMO qualification>
```

Only approved official HTTPS listing URLs pass configuration validation. Enabling
these values changes the authenticated helper page to offer store links; it does
not publish a package. Production configuration and the later release still
require their own explicit owner authorization and release checks. Do not set a
Firefox listing URL for the current candidate.

## Official publisher references

- [Register a Chrome Web Store developer account](https://developer.chrome.com/docs/webstore/register)
- [Chrome Web Store image requirements](https://developer.chrome.com/docs/webstore/images)
- [Chrome Web Store privacy policy requirements](https://developer.chrome.com/docs/webstore/program-policies/privacy)
- [Register as an Edge extension developer](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/create-dev-account)
- [Publish an Edge extension](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/publish-extension)
- [Firefox Add-ons distribution](https://extensionworkshop.com/documentation/publish/distribute/)
- [Firefox Manifest V3 add-on ID requirement](https://extensionworkshop.com/documentation/develop/extensions-and-the-add-on-id/)
- [Firefox built-in data collection consent](https://extensionworkshop.com/documentation/develop/firefox-builtin-data-consent/)

## Scoped validation evidence

The exact accepted source, package and fixture hashes, synthetic browser/OCR
readback, review scope and exclusions are recorded in
[kad-helper-validation-2026-10-21.json](kad-helper-validation-2026-10-21.json).
This evidence does not establish official-store installation, Windows/Edge or
live KAD acceptance, and does not authorize production deployment.
