# External integrations: KAD helper and SROArbitr

## Retired acquisition experiments

The unused public-source debtor search and the Safari/SafariDriver KAD diagnostic were removed on 2026-10-13. They are not a fallback when the KAD helper stops. Schema head `c3a8e1d04b57` drops that intake label and its document provenance column. Historical KAD outbox intents stay withheld and are not dispatched.

The local OCR/parser path stays on this machine. Never copy real case identifiers, documents, OCR text or extracted personal data into source, tests, logs or Memory Bank. The owner-authorized sample documents stay local.

## KAD ordinary-browser helper and retired MKAD adapter

The owner's request reinstated KAD through the ordinary-browser helper in `browser-extension/kad-helper/`, with authenticated `/kad-helper/import` and local OCR. The current helper is 0.2.10 and runs in Firefox and Chrome. A bare 502/503/504 gateway document is reloaded at most twice per search page and twice per card page, with a 2s then 6s pause, and only before Find is submitted. Challenge, captcha and rate-limit pages are not reloaded. A gateway page after Find stops without another search. Version 0.2.6 completed one requested case/date acquisition and strictly unsaved SRO preview. Candidate links/date and parsed case/date remain bound; missing or ambiguous selection, known access checks and acquisition errors stop explicitly. See `.now.md` and the helper README for the current limits.

The older MKAD/WebDriver adapter and worker remain retired. Legacy database enum labels, event history and outbox support preserve earlier records; old KAD acquisition outbox intents stay withheld and are never dispatched. This does not disable browser-helper PDF imports.

## SROArbitr — strictly unsaved form preview

SROArbitr is ASP.NET WebForms at `https://sroarbitr.sro-service.ru/default.aspx`; network reachability depends on the owner's network. Use IDs in `sro_robot/platform/pages.py`, not labels, and reacquire locators after postbacks. Keep the private Playwright profile out of Git and logs.

The owner reports staff confirmed that person-list **Add** opens a blank card and does not persist it; persistence occurs only on **Save**. The owner authorizes this narrow test: open a new blank card with Add, fill approved fields, inspect the real form, then Close without saving. Never click Save/Send, alter an existing record, or invoke uncertain controls (including nested address Add/Save). The application guard permits only required login, blank-card Add and Close requests in prepare-only mode; all other SRO POSTs are blocked before network. No SRO write result may be inferred from filling or closing a form.

Stable identifiers documented from inspection:

- Login: `#Login1_UserName`, `#Login1_Password`, `#Login1_LoginButton`.
- Person list: `/register/person_list.aspx`.
- New person: open from the list's Add control; never construct an existing-record URL.
- Person identity/status fields live under `TabPanel1`: `txt_pn_name_last`, `txt_pn_name_first`, `txt_pn_name_mid`, `txt_id_inn`, `dd_pn_sex`, `txt_pn_comment`, `dd_dic_id`.
- Save: `#ctl00_pContent_BtnItemPersonOk` (prohibited). Close: `#ctl00_pContent_BtnItemPersonCancel` (allowed only in prepare-only mode; confirm close without saving).

The inspected blank person form has no birth-date, SNILS or birthplace fields. Show SNILS for employee review only. Do not show or extract date of birth or birth place, and do not invent destination controls. Address entry is not part of the allowed preview because nested address actions have not been verified safe. Real form preparation was previously exercised through the main SRO Robot worker and closed without saving; that evidence does not mean a record exists.
