# External integration boundaries

## KAD

`browser-extension/kad-helper` and authenticated helper/import routes are the
supported KAD path. The browser performs ordinary navigation, preserves candidate
metadata and transfers the selected complete PDF for local processing. Protocol,
capability, selected-document identity, session ownership and import bounds are
validated. Follow `../docs/KAD_HELPER_ADMIN.md` and the extension README for current
connection/selection behavior; browser chronology alone cannot prove a full case
history or the legally correct act. Stop on CAPTCHA, access checks or acquisition
errors. Historical MKAD/public-source worker jobs remain withheld, not fallback.

## SROArbitr

The website uses ASP.NET WebForms. Source selector IDs live in
`sro_robot/platform/pages.py`; use IDs, wait for postbacks and reacquire locators.
Employee login verifies the ordinary remote account; imported session state must
belong entirely to the exact SRO origin and is encrypted before local storage.
Network/account availability and session expiry are external failure boundaries.

Preserve prepare-only, recognition-only, account-lock, owned-record identity,
baseline/readback, one-shot request and unknown-outcome guards. An internal save,
form fill, Close, or successful HTTP response is not verified external persistence.
The default client blocks mutating controls. Blank-person Add/fill/inspect/Close
permission is narrow and does not authorize Save/Send, an existing record,
assignment Add or nested address actions. Development/cleanup does not broaden it.

Current guarded adapters for people/entities/addresses and operation reconciliation
are retained; their synthetic checks protect boundaries without making real writes.
Do not retry an unknown external result until read-only reconciliation is complete.
Never use real registry entries as fixtures or bypass authentication challenges.

Assignment selectors/date semantics and safe external assignment creation remain
unqualified. Keep internal assignment drafts separate. See `../docs/APPOINTMENTS.md`.
