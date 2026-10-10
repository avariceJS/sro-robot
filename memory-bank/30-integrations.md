# External systems: MKAD and SROArbitr

## MKAD acquisition

The requested source is `https://mkad.arbitr.ru/Kad`. The owner reports permission from a cooperating court for the browser acquisition workflow until an API is available; this is reported business context, not independent proof of every site's technical rule.

Observed public page structure from static inspection:

- Search form action `/Kad/Search`.
- Case input `input[name="CaseNumbers[]"]`.
- Submit input with value `Найти`.
- Case card response path `/Kad/Card`.
- The site loads a CAPTCHA/challenge script; do not bypass it.

Current dirty-worktree adapter at `sro_robot/kad.py` targets these elements, waits for a `/Kad/Card` response, observes PDF responses/downloads/popups and fails closed on empty/challenge pages. A headless read-only attempt returned an empty body. End-to-end search, candidate selection, PDF acquisition and parser results are not verified.

Known adapter risks: first visible published/PDF candidate may be the wrong act; fixed 1.2-second wait; downloaded act's case number is not matched to requested case; multiple candidate acts and pages are not semantically modeled. Replace arbitrary timing with observed response/download conditions. Preserve candidate list, source identifier/link, publication date/type, selected-act rationale and original downloaded file. Never treat latest/first as a semantic rule without evidence.

## SROArbitr safety boundary

Base URL: `https://sroarbitr.sro-service.ru/default.aspx`. It is ASP.NET WebForms. Network access is IP-dependent; some agent/data-center IPs return connection refused while the owner's network has worked. Do not infer code failure until network source is checked.

No write integration has been verified against a real Save. The project owner prohibits all real registry mutations. This agent must not invoke `--live`, press Save/Send, delete/update records, or test a real write. Add may create a draft; do not click it unless non-mutating behavior has been proven in an isolated fake. Read-only navigation is allowed only after checking effects.

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

Use selector dictionaries in `sro_robot/platform/pages.py` and inspect-derived identifiers, never labels. “Комментарий” previously matched a checkbox rather than the textarea. There are separate Save controls for address dialog and person card; address save must be scoped from `dd_addr_type`. Add may create an incomplete draft. WebForms postbacks invalidate stale locators; settle and reacquire. Prefer direct URLs over duplicated menu labels.

Assignment list is not a person registry: it lacks the person search field. Inspect an already-open card without clicking Add; derive selectors from live inspection before implementing a scenario.

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

Current code does not implement durable attempts, reservations, unknown state or read-back verification. The previous failed SRO job remains unreconciled.
