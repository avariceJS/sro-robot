# Internal appointment preparation

## Implemented workflow

`DebtorCardDraft` and `AssignmentDraft` are separate internal records. An assignment always references one debtor card; several assignments can reference the same debtor. The system does not merge or deduplicate assignments by case number or debtor identity.

OCR may conservatively prepare a debtor card, but neither that preparation nor saving a debtor card implies a business decision to make an appointment. The person-card Create assignment action saves and validates the editable card and creates the separate assignment in one transaction; there is no additional card-confirmation button. With an eligible approved source, the first assignment is linked to that source and card edits update its native approval audit. Unreviewed cards cannot seed an assignment without this validation. The explicit source-task action is idempotent for that document; another source task may create another independent assignment for the same debtor and case. The draft copies only the card's case number and court when the stored extraction has no warning and confidence at least 0.7 (employee-confirmed values are accepted). Applicant, applicant address, insolvency practitioner, comments, protocol/refusal/archive/publication fields and event-specific dates remain blank. The existing parser does not provide role-bound evidence for those assignment fields, and the business meaning of several dates is not established.

The bulk dashboard shows the number of explicitly created source-linked assignment drafts and links each document to its draft. If a reviewed document belongs to a debtor already represented by another reviewed internal card, the employee can select that card from the task page. The operation requires an approved task with a current approval digest, explicitly records the employee as author, links the assignment to that source task and uses only confirmed case details or warning-free, high-confidence court text. No automatic match by name or case number occurs. A source task can seed only one assignment; a different task can seed another assignment for the same debtor and case.

Employees share the debtor-card and assignment lists. Assignment search supports debtor name and case number; status filters cover draft, awaiting information and ready for review. The editor permits incomplete values, lists unfilled fields as items to verify for the individual assignment, and shows source task, debtor link, revision history and the last editing employee. Concurrent edits use row locking and optimistic version checks. PostgreSQL records each assignment revision with its actor and prevents revision mutation. These are internal SRO Robot records only.

## Live SROArbitr inspection (2026-10-16)

The earlier report incorrectly treated the absence of an already-open employee browser session as proof that no authorized login was available. The local account pair remained in the private `.env`, but `Config.load()` deliberately excludes `SRO_LOGIN` and `SRO_PASSWORD` from shared runtime settings. A one-shot call to `authenticate_sro_credentials()` successfully verified the account; only the resulting storage-state bytes were held in process memory, then discarded. No credential, cookie, token, response body or screenshot was written. This did not create a durable SRO Robot employee session.

Using that ephemeral state with the existing `prepare_only` network guard, a read-only GET opened `/register/assignment_list.aspx` (`Назначения АУ`). The search type choices were `№`, `АУ`, `АС`, `Д`, `З`, `ИНН`, `Исх` and `Вх`; `Д` searches assignments by debtor. A synthetic-only query was submitted through the observed `BtnSearch` postback; its exact event target, selected `Д` filter and unique synthetic sentinel were allowlisted for this probe. The result was empty. No office identifier was searched. Existing assignment records were opened only through links containing an existing-record query; only structural field identifiers and labels were retained.

The existing assignment card exposes fields for case identifiers and a date, court, incoming number, judge, procedure/debtor classifications, debtor and practitioner selector controls, applicant and applicant address, comments/annotation/creditors, outgoing number, two protocol rows, refusal/archive/publication flags, and several event dates. Stable observed control IDs are documented in `memory-bank/30-integrations.md`. The main field groups were:

| Existing-card control IDs (suffix) | Observed caption / purpose |
| --- | --- |
| `txt_as_file_num_px`, `txt_as_file_num`, `dt_as_file_date` | Case number/date group; exact distinction between the two number fields was not qualified |
| `ctrl_select_court_txt_in_doc` | Court selector textbox |
| `ctrl_selector1_txt_in_doc` | Incoming request number |
| `txt_as_judge`, `txt_as_jude_code` | Judge and judge code |
| `dd_as_proc_type`, `dd_as_debtor_cat`, `dd_as_debtor_type`, `dd_as_type_control` | Procedure/debtor category/type selectors |
| `ctrl_select_debtor_txt_in_doc` | Debtor selector textbox |
| `txt_as_declarant_name`, `txt_as_declarant_address` | Applicant and applicant address |
| `ctrl_as_owner_txt_in_doc` | Practitioner/owner selector textbox |
| `txt_as_text`, `txt_as_anno`, `txt_as_creditors` | Comment, annotation and creditors textareas |
| `ctrl_selector2_txt_in_doc` | Outgoing number |
| `txt_as_protocol_num_1/2`, `txt_as_protocol_pt_1/2`, `dt_as_protocol_date_1/2` | Two protocol rows |
| `cb_as_reject`, `cb_as_archive`, `cb_as_visible` | Refusal, archive and publication controls |
| `dt_as_date_ass`, `dt_as_date_next`, `dt_as_date_stop`, `dt_as_date_finish`, `dt_as_date_meeting`, `dt_as_date_suspension` | Date inputs whose precise legal meanings are not yet verified |

These are fields present on an existing record, not confirmation that each has the expected semantics for a new appointment or that filling them is safe. Selector-popup mechanics, hidden external IDs and date meanings remain unqualified. The internal fields `procedure_date`, `practitioner_appointment_date` and `next_hearing` are deliberately not mapped to these date inputs until staff confirm their legal meaning.

The list's no-query `assignment_card.aspx` Add link was observed but not followed. Its effect is unknown. No new assignment form was opened, filled, closed, saved or otherwise acted upon. Therefore safe blank-form opening and live assignment preparation remain unconfirmed and unavailable. The earlier report was wrong to say that the authorized credentials could not be used; it was right that no session was bound to a local employee session at that point.

## External SROArbitr boundary

The employee login and encrypted per-user SRO session are already implemented. A direct one-shot authentication and read-only assignment inspection succeeded, but that ephemeral state was not bound to an employee's SRO Robot login session. The existing strictly unsaved browser adapter has selectors and guards for a new person card only. No assignment-form field selectors or safe Add/empty-form behavior have been confirmed. The assignment registry inspector deliberately reads the list and does not press Add because its effect may create a record. Therefore this release does not offer assignment-form preparation and has not opened or filled a live assignment form. Do not infer an external SRO record from an internal draft.

The internal user interface states this boundary explicitly. The current SRO client guard remains unchanged; adding an assignment-specific preview requires a verified personal session, read-only field inspection, and affirmative evidence that opening a new form cannot persist a record. If the platform's Add control creates a draft or changes state, a live open/fill/close test is not permitted under the present safety rules.

## Published help-page fields and current internal draft

On 2026-10-17, the static SROArbitr help viewer at `/docs/#/persons` was read
in the already-authorized browser. Its appointment description lists a case
date, judge, incoming request, procedure/debtor/appointment reference values,
debtor and practitioner selectors, applicant/address, creditors, annotation,
assets, additional-insurance flag, outgoing number, protocol rows,
remuneration, suspension, archive and publication. It describes separate dates
for appointment, next hearing, termination/completion, creditors' meeting and
release/removal, plus calendar dependencies and planned/actual event dates.
This is published documentation only: no current live form was opened, and
these semantics are not approved office rules. Further detail and evidence
qualification are in [SROARBITR_READONLY_RESEARCH.md](SROARBITR_READONLY_RESEARCH.md).

SRO Robot's internal editor now carries corresponding resumable text fields for
the documented case/judge/classification, applicant, debtor/practitioner-related
context, creditors, assets, protocols, remuneration, distinct event dates,
suspension and flags. `procedure_date` is retained for old drafts but marked
ambiguous. Reference-valued fields are free-text internal draft values because
the SRO dictionaries have not been safely read. No calendar dependency engine,
downstream date recomputation, notifications, external selector identity, or
SRO write is implemented. A field's presence does not mean it is required or
legally interpreted.

Assignments seeded from reviewed person, organization or IP cards carry only
the confirmed case number and court. Employee-created values are saved with
revision history and conflict detection, and the task/card/assignment links
remain internal. The full synthetic UI and PostgreSQL workflow covers entity
card review and assignment creation; live SRO form preparation and SRO read-back
remain untested and disabled.

## Migration and verification

Migration `a6c2d8e41b90` adds nullable `source_task_id`, its task foreign key and a unique constraint. Existing internal drafts remain intact and unlinked. The migration is forward-only because automatic downgrade would discard assignment provenance. Runtime schema checks verify the new uniqueness and foreign-key guards.

Synthetic unit and PostgreSQL checks cover bulk OCR to debtor card, a separate employee-confirmed assignment action, idempotent replay by source task, multiple assignments for the same debtor/case, explicit employee linking from another task, incomplete drafts, concurrent stale edits and actor-attributed revision history. A real authorized account performed a one-shot login plus list/existing-card GET and an exact synthetic-only assignment search. No real office data was searched and no real record was changed. These checks do not verify a live SROArbitr assignment form.
