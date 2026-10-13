# External integrations: KAD helper, experimental Fedresurs and SROArbitr

## Fedresurs — preserved experimental backend

The active public search UI is `https://bankrot.fedresurs.ru/bankrupts`. It accepts debtor name, INN, or case number. A visible browser search using an owner-authorized real case returned an exact debtor result and a public profile link. In that profile, the “Сведения о банкротстве” section exposed three publication links. One publication explicitly labelled as an arbitration-manager appointment matched the requested case and exposed one PDF download control. The PDF was downloaded through the visible UI and parsed locally; the extracted case matched and required debtor fields were present.

The current worker uses that browser flow, keeps source/candidate URLs and the selection reason, and fails closed on a challenge, missing or duplicate exact search result, zero or multiple appointment publications, cross-host URL, multiple attachments, invalid/oversized PDF, or case mismatch. It never calls undocumented endpoints or changes browser fingerprints. Do not solve CAPTCHA or bypass access controls. If the site layout changes, stop acquisition and update the selector only after fresh visible inspection.

Coverage is limited to the verified publication pattern. A profile may have no matching message, multiple matching/corrected messages, unsupported attachment layout, or no relevant court act. Those states are not “no case” and must remain explicit failures/manual review; do not select first/latest. Interactive employee selection when multiple appointment publications exist is not implemented yet. Fedresurs publications and its legal-case document index may expose some court documents, but this does not establish that it has every act available in KAD. The official Fedresurs REST specification reviewed in this task does not document case-number search. Use only documented and ordinary public UI behavior until authorized API documentation is supplied.

The local OCR/parser path stays on this machine. Never copy real case identifiers, documents, OCR text or extracted personal data into source, tests, logs or Memory Bank. The owner-authorized sample documents stay local.

## KAD ordinary-browser helper and retired MKAD adapter

The owner's later request reinstated KAD through the ordinary Firefox helper in `browser-extension/kad-helper/`, with authenticated `/kad-helper/import` and local OCR. Version 0.2.6 completed one requested case/date acquisition and strictly unsaved SRO preview. Candidate links/date and parsed case/date remain bound; missing or ambiguous selection, known access checks and acquisition errors stop explicitly. See `.now.md` and the helper README for the current limits.

The older MKAD/WebDriver adapter and worker remain retired. Legacy database enum labels, event history and outbox support preserve earlier records; old KAD acquisition outbox intents stay withheld and are never dispatched. This does not disable browser-helper PDF imports. The existing Fedresurs adapter/API is an independent experimental path preserved in this checkpoint, not a replacement for the owner-requested KAD workflow.

## SROArbitr — strictly unsaved form preview

SROArbitr is ASP.NET WebForms at `https://sroarbitr.sro-service.ru/default.aspx`; network reachability depends on the owner's network. Use IDs in `sro_robot/platform/pages.py`, not labels, and reacquire locators after postbacks. Keep the private Playwright profile out of Git and logs.

The owner reports staff confirmed that person-list **Add** opens a blank card and does not persist it; persistence occurs only on **Save**. The owner authorizes this narrow test: open a new blank card with Add, fill approved fields, inspect the real form, then Close without saving. Never click Save/Send, alter an existing record, or invoke uncertain controls (including nested address Add/Save). The application guard permits only required login, blank-card Add and Close requests in prepare-only mode; all other SRO POSTs are blocked before network. No SRO write result may be inferred from filling or closing a form.

Stable identifiers documented from inspection:

- Login: `#Login1_UserName`, `#Login1_Password`, `#Login1_LoginButton`.
- Person list: `/register/person_list.aspx`.
- New person: open from the list's Add control; never construct an existing-record URL.
- Person identity/status fields live under `TabPanel1`: `txt_pn_name_last`, `txt_pn_name_first`, `txt_pn_name_mid`, `txt_id_inn`, `dd_pn_sex`, `txt_pn_comment`, `dd_dic_id`.
- Save: `#ctl00_pContent_BtnItemPersonOk` (prohibited). Close: `#ctl00_pContent_BtnItemPersonCancel` (allowed only in prepare-only mode; confirm close without saving).

The inspected blank person form has no birth-date, SNILS or birthplace fields. Show those values for employee review only; do not invent destination controls. Address entry is not part of the allowed preview because nested address actions have not been verified safe. Real form preparation was previously exercised through the main SRO Robot worker and closed without saving; that evidence does not mean a record exists or that the current Fedresurs task completed SRO preparation.

## Primary references

- [Fedresurs public bankruptcy search](https://bankrot.fedresurs.ru/bankrupts)
- [Fedresurs official REST specification](https://download.fedresurs.ru/HELP/3.%20BANKROT/10.%20MS/10.3%20SERVICE_REST/Service_rest_1.0.2.pdf)
