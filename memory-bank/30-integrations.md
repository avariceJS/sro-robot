# External integrations: KAD helper and SROArbitr

## Retired acquisition experiments

The unused public-source debtor search and the Safari/SafariDriver KAD diagnostic were removed on 2026-10-13. They are not a fallback when the KAD helper stops. Schema head `c3a8e1d04b57` drops that intake label and its document provenance column. Historical KAD outbox intents stay withheld and are not dispatched.

The local OCR/parser path stays on this machine. Never copy real case identifiers, documents, OCR text or extracted personal data into source, tests, logs or Memory Bank. The owner-authorized sample documents stay local.

## KAD ordinary-browser helper and retired MKAD adapter

The owner's request reinstated KAD through the ordinary-browser helper in `browser-extension/kad-helper/`, with authenticated `/kad-helper/import` and local OCR. The current helper is 0.2.11 and runs in Firefox and Chrome. A bare 502/503/504 gateway document is reloaded at most twice per search page and twice per card page, with a 2s then 6s pause, and only before Find is submitted. Challenge, captcha and rate-limit pages are not reloaded. A gateway page after Find stops without another search. Version 0.2.6 completed one requested case/date acquisition and strictly unsaved SRO preview. Candidate links/date and parsed case/date remain bound; missing or ambiguous selection, known access checks and acquisition errors stop explicitly. Closing the owned KAD tab now leaves the service run stopped and blocks import. See `.now.md` and the helper README for the current limits.

The older MKAD/WebDriver adapter and worker remain retired. Legacy database enum labels, event history and outbox support preserve earlier records; old KAD acquisition outbox intents stay withheld and are never dispatched. This does not disable browser-helper PDF imports.

## SROArbitr — employee login and strictly unsaved form preview

SROArbitr is ASP.NET WebForms at `https://sroarbitr.sro-service.ru/default.aspx`; network reachability depends on the owner's network. Use IDs in `sro_robot/platform/pages.py`, not labels, and reacquire locators after postbacks. Keep the private Playwright profile out of Git and logs.

The local candidate's single `/login` accepts an employee's SROArbitr login and password. `sro_robot.platform.authentication.authenticate_sro_credentials` uses a fresh HTTPS-only browser context, allows one ordinary login POST, and requires the observed `Вы вошли как` marker plus an SRO-domain cookie. CAPTCHA, additional verification, changed page structure, or unavailable responses stop without a bypass or retry. Runtime config deliberately does not load global `SRO_LOGIN` / `SRO_PASSWORD`; the old persistent profile is not used for web preview jobs.

On successful login, only the verified external username is used as the local actor key; no stable SRO user ID has been established. Login-state JSON is checked for the exact SRO cookie domain and local-storage origin (all cookies/origins must belong to the SRO host; unknown fields are rejected), then encrypted with AES-GCM before storage in the corresponding `login_session` row. HKDF derives the AES key from `APP_SECRET_KEY`; random nonce and associated data bind ciphertext to the local employee and login-session IDs. The state has an eight-hour maximum lifetime, is absent from RQ/Redis and the employee browser, and is deleted by local logout. Local logout does not call an SRO-side logout endpoint; it removes the app's ability to use the remote session. Workers decrypt only the requesting employee's newest active state and verify the live account marker before using it. External session timeout at the SRO site may be shorter; the next preview fails closed and revokes that local session so the employee must sign in again.

The adapter and encrypted-storage behavior have synthetic tests and an isolated PostgreSQL 16 migration/isolation drill. Live qualification then completed with the authorized owner account: after one initial `unavailable` attempt and a no-credential form check, the new adapter authenticated successfully twice; a fresh Chromium context reused captured session state; the owner-authorized blank person card opened, seven synthetic fields were read back, and the form closed with `saved=false`. No real record was searched or changed. CAPTCHA/MFA, SRO-side logout, external expiry and a second actual employee account remain untested. The candidate is not deployed.

The owner reports staff confirmed that person-list **Add** opens a blank card and does not persist it; persistence occurs only on **Save**. The owner authorizes this narrow test: open a new blank card with Add, fill approved fields, inspect the real form, then Close without saving. Never click Save/Send, alter an existing record, or invoke uncertain controls (including nested address Add/Save). The application guard permits only required login, blank-card Add and Close requests in prepare-only mode; all other SRO POSTs are blocked before network. No SRO write result may be inferred from filling or closing a form.

Stable identifiers documented from inspection:

- Login: `#Login1_UserName`, `#Login1_Password`, `#Login1_LoginButton`.
- Person list: `/register/person_list.aspx`.
- New person: open from the list's Add control; never construct an existing-record URL.
- Person identity/status fields live under `TabPanel1`: `txt_pn_name_last`, `txt_pn_name_first`, `txt_pn_name_mid`, `txt_id_inn`, `dd_pn_sex`, `txt_pn_comment`, `dd_dic_id`.
- Save: `#ctl00_pContent_BtnItemPersonOk` (prohibited). Close: `#ctl00_pContent_BtnItemPersonCancel` (allowed only in prepare-only mode; confirm close without saving).

The inspected blank person form has no birth-date, SNILS or birthplace fields. Show SNILS for employee review only. Do not show or extract date of birth or birth place, and do not invent destination controls. Address entry is not part of the allowed preview because nested address actions have not been verified safe. Real form preparation was previously exercised through the main SRO Robot worker and closed without saving; that evidence does not mean a record exists.

Read-only assignment inspection was performed on 2026-10-16 after a one-shot account login through `authenticate_sro_credentials()`. The cookie state stayed in process memory and was discarded; it was not attached to a local employee login row. Assignment list: `/register/assignment_list.aspx`, title `Назначения АУ`. Search controls are `#ctl00_pContent_TabContainer1_TabPanel1_ctrl_search_type_dd_type` and `#ctl00_pContent_TabContainer1_TabPanel1_ctrl_search_type_txtSearch`; observed search types are `№`, `АУ`, `АС`, `Д`, `З`, `ИНН`, `Исх`, `Вх`. The read-only search event target is `ctl00$pContent$TabContainer1$TabPanel1$ctrl_search_type$BtnSearch`. A search for a unique synthetic sentinel using type `Д` returned no rows. It did not search a real office debtor.

An existing assignment record was opened by its existing-record GET link; no values were copied out. Field IDs observed include `txt_as_file_num_px`, `txt_as_file_num`, `dt_as_file_date`, `ctrl_select_court_txt_in_doc`, `ctrl_selector1_txt_in_doc` (incoming), `ctrl_select_debtor_txt_in_doc`, `txt_as_declarant_name`, `txt_as_declarant_address`, `ctrl_as_owner_txt_in_doc`, `txt_as_text`, `ctrl_selector2_txt_in_doc` (outgoing), paired `txt_as_protocol_num_*` / `txt_as_protocol_pt_*` / `dt_as_protocol_date_*`, `cb_as_reject`, `cb_as_archive`, `cb_as_visible`, and `dt_as_date_*` event fields. Form captions/ID structure do not establish which date means procedure, appointment, next hearing or another legal event; keep them review-only until qualified.

The no-query link to `/register/assignment_card.aspx` was not followed: it is the Add path and its server-side effect is unverified. No new form was opened or filled. The existing person-card Add permission does not apply to assignment forms. Do not press Add or attempt assignment-form filling until the no-persistence opening path is independently confirmed. See `../docs/APPOINTMENTS.md`.
