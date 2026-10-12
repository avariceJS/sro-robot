# External systems: MKAD and SROArbitr

## MKAD acquisition

The requested source is `https://mkad.arbitr.ru/Kad`. The owner reports permission from a cooperating court for the browser acquisition workflow until an API is available; this is reported business context, not independent proof of every site's technical rule.

Observed public page structure from static inspection:

- Search form action `/Kad/Search`.
- Case input `input[name="CaseNumbers[]"]`.
- Submit input with value `Найти`.
- Case card response path `/Kad/Card`.
- The site loads a CAPTCHA/challenge script; do not bypass it.

Current adapter at `sro_robot/kad.py` targets the mobile page, waits for `/Kad/Card`, observes PDF responses/downloads/popups and fails closed on empty/challenge pages. On 2026-10-12 the workstation tried both owner-supplied real designation documents in `samples/designation/`. Their case-number lookups reached the official page but yielded no candidate inventory: the page body was empty and the browser console reported `WebAssembly.RuntimeError: unreachable`; the desktop page showed a current service-difficulty notice. One attempt emitted `/Kad/Search` but its response had no usable body. No CAPTCHA/access challenge was solved or bypassed. Search, candidate inventory and PDF acquisition remain externally blocked. Preserve the original documents and identifiers locally; do not copy extracted personal data here.

Known adapter risks: current code picks the first visible publication/PDF candidate, which is not a safe act-selection rule; it also uses a fixed 1.2-second wait, does not match the downloaded act's case number to the request, and does not model candidate pages semantically. Screenshots from the owner show multiple kinds of acts/events in one case. Do not auto-select by first/latest order or generic `определение` label. Require a bounded complete candidate inventory and explicit target operation; metadata narrows candidates, local content and role evidence establish relevance, and unresolved/multiple candidates go to review. Per-operation download budget: five; per package: ten. Preserve candidate IDs, event association, source link, act/publication dates separately, selection rationale, rejected reasons and original bytes. Rules for card basis, manager appointment/replacement, corrections and act priority remain unresolved.

## SROArbitr safety boundary

Base URL: `https://sroarbitr.sro-service.ru/default.aspx`. It is ASP.NET WebForms. Network access is IP-dependent; some agent/data-center IPs return connection refused while the owner's network has worked. Do not infer code failure until network source is checked.

No real Save/write integration has been exercised or enabled. On 2026-10-12 the workstation reached the actual SROArbitr login page (HTTP 200 with redirect), and the configured private Playwright profile authenticated successfully. Read-only navigation opened `/register/person_list.aspx`. After the owner clarification, the authorized real test path `Add -> fill -> inspect -> Close without saving` was exercised twice through the main SRO Robot worker and UI.

**Owner clarification, 2026-10-12:** Staff confirmed that this `Add` action only opens an empty person card and does not persist a record; the record is created only by `Save`. The owner explicitly authorizes the real test sequence `Add -> fill fields -> inspect -> Close without saving`. Never click `Save`, change an existing record, or use another control whose side effects are uncertain without separately confirming its safety. This authorization is narrow to preparing a new unsaved card and closing it. It does not authorize general `live` mode, writes, nested address Add/Save actions, or other registry mutations. Keep the default mutation guard fail-closed and implement any preview permission as an explicit, narrowly scoped operation that permanently blocks Save.

### Known person-card paths and selectors

- Login: `/default.aspx`; selectors `#Login1_UserName`, `#Login1_Password`, `#Login1_LoginButton`.
- Person list: `/register/person_list.aspx`.
- Person card: `/register/person_card.aspx?id=GUID`.
- Firm list: `/register/firm_list.aspx`.
- Assignment list: `/register/assignment_list.aspx`.
- Person search: `#ctl00_pContent_ctrl_search_txtSearch`.
- Person fields use `TabPanel1`: `txt_pn_name_last`, `txt_pn_name_first`, `txt_pn_name_mid`, `txt_id_inn`, `dd_pn_sex`, `txt_pn_comment`, `dd_dic_id`.
- Address dialog is under `ctrl_contact1`; fields include `dd_addr_type`, `txt_addr_index`, `txt_addr_country`, `txt_addr_area`, `txt_addr_dist`, `txt_addr_city`, `txt_addr_street`, `txt_addr_build`, `txt_addr_box`, `txt_addr_room`.
- Person status includes `Должник`; address types include `Адрес регистрации` and `Фактический адрес`; sex values include Latin `M` and Cyrillic `Ж`.

Use selector dictionaries in `sro_robot/platform/pages.py` and inspect-derived identifiers, never labels. “Комментарий” previously matched a checkbox rather than the textarea. WebForms postbacks invalidate stale locators; settle and reacquire. Prefer direct URLs over duplicated menu labels. Staff confirmed person-list `Add` only opens a blank unsaved card; the new-card URL contains a GUID, but its identity inputs remain empty except for the site's `Фамилия не указана` placeholder. Person-card Save is `#ctl00_pContent_BtnItemPersonOk`; Close is `#ctl00_pContent_BtnItemPersonCancel` and asks for confirmation. In `prepare_only` mode, the network guard allows only login, person-list Add, and person-card Close; all other SRO POSTs are aborted before network, including both person Save and nested address controls.

The new-person general form exposes name, INN, sex, comment and status (`Должник`); the inspected form has no date-of-birth, SNILS or birthplace control. The operator review screen now displays those OCR fields with an explicit review-only notice. They are not transferred to SRO. Address data is also visible to the operator but its nested address Add/Save workflow is not exercised or used because the owner has not separately confirmed those controls' effects. The prepared preview verifies every filled text input plus selected sex/status by reading the real SRO controls back before taking the screenshot.

**Verified real safe preview, 2026-10-12:** two owner-provided image documents were uploaded through the main service, processed by local Tesseract/OCR, parsed and shown as editable review fields. After operator approval, task 2 opened the actual authenticated SRO new-person form; Playwright filled the extracted case comment, debtor name, INN, sex and debtor status. The owner observed/authorized `Add -> fill -> inspect -> Close`. The main review page displayed the real SRO screenshot and only offered “close without writing”; closing completed the `sro_preview` RQ job and left the task `dry_run=True`, decision `close`, with no error. No SRO Save request was sent. A prior first attempt failed before a screenshot because the blank-card guard incorrectly treated the generated URL GUID as an existing-record ID. It was reconciled with read-only evidence, the false guard was corrected, and the explicit safe retry succeeded; the failed RQ registry entry is retained and was not rerun. The actual successful preparation is not evidence of a real persisted record or Save integration.

Assignment list is not a person registry: it lacks the person search field. The owner authorization above permits opening a new person card only through the verified `Add` path; derive selectors from live inspection and reacquire locators after postbacks.

## Owner-provided local acquisition samples

The owner identified two real judicial designation documents in `samples/designation/` for local OCR/extraction and for extracting their case numbers to search the real MKAD. Their document bytes and extracted personal data must stay local and must not be copied into source, tests, logs, or this Memory Bank. Search results must be fully inventoried and the relevant act selected explicitly; the existing latest/first-act shortcut is not qualified.

## SRO write result contract

Required before any future live action is enabled:

1. Persist an attempt/submission marker before final Save, bound to attempt ID, payload digest/revision and debtor identity.
2. After marker, all interruption/timeout/browser/worker-loss paths become durable `write_result_unknown`; do not automatically repeat Save.
3. Reserve tenant/registry + identity + create operation atomically across tasks; reservation survives worker-lease expiry.
4. Bind operator approval to exact payload. Current implementation does this with a canonical value digest in an approval audit event.
5. On an unknown attempt, reconcile read-only. An empty result is not proof of failure.
6. Reopen a uniquely matched record and semantically verify identity and approved fields before `saved_verified`.
7. Distinguish `dry_run`, `saved`, and `verified`; a click/current URL/form disappearance is not evidence.
8. Serialize writes per SRO account/session until session isolation and task ownership are demonstrated.

Current code does not implement durable live-write attempts, reservations, unknown-result reconciliation or saved-record verification. The safe preview path is separate from the general `live` write path and cannot invoke Save. The prior failed preview job described above was reconciled and remains retained as a failed registry entry for audit.
