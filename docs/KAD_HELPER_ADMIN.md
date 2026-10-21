# KAD helper administration

This page is an administrator runbook for distributing the store-approved
extension to managed Windows Chrome and Edge. It is a target procedure: no
publisher listing, approved store ID, Windows Chrome/Edge live KAD pilot, or
enterprise policy deployment has been verified yet. Do not deploy the placeholders
below. Obtain the final store ID and listing URL from the approved listing and
authorize the production release separately.

## Before rollout

1. Confirm the exact `0.3.0` store candidate has passed the publisher's review
   and that the listing displays the approved Russian copy and privacy policy.
2. Record the store-assigned extension ID, official listing URL, package hash,
   reviewed version, and approval date in the change record. Chrome and Edge have
   distinct IDs and listings even when built from this repository.
3. Complete a bounded Windows pilot in the managed browser and office network.
   Verify extension installation/update, the browser notice, an explicitly chosen
   synthetic or owner-approved KAD PDF, receipt reconciliation, and local task
   provenance. Live KAD availability and selectors are not qualified by offline
   tests. The visible inventory can be incomplete and still permit an explicit
   employee choice. Stop on a missing or changed candidate, access challenge,
   acquisition failure, or unknown receipt; reconcile uncertain receipts first.
4. Confirm the HTTPS office origin, authenticated access, local OCR/parser, and
   published listing URL configuration. Never enable a developer ZIP as staff
   distribution.
5. Define the rollout organizational unit/group and rollback owner. Force-installed
   extensions are managed centrally and users cannot remove them; use a small
   pilot scope before expanding.

## Chrome Enterprise

In Google Admin Console, use **Devices → Chrome → Apps & extensions** (or the
managed Chrome browser Apps & extensions page), select the pilot users or enrolled
browsers, and add the approved Chrome Web Store item with **Force install**. Record
the store ID assigned after approval. Do not substitute an unofficial CRX or a
developer package. Google documents Chrome Web Store force installation and
organization scoping in [Automatically install apps and extensions](https://support.google.com/chrome/a/answer/6306504).

For a Group Policy/registry based Windows deployment, distribute the current
Google Chrome policy template from the organization's approved admin channel and
configure `ExtensionInstallForcelist` under the managed Chrome policy. One list
entry is:

```text
<APPROVED_CHROME_EXTENSION_ID>;https://clients2.google.com/service/update2/crx
```

Keep the ID as a placeholder until store approval. Verify that the managed client
reports the intended item and update source in `chrome://policy` and the extension
page. Chrome periodically checks the store for updates; an active extension may
finish the update after it becomes idle or the browser restarts.

## Microsoft Edge for Business

In the organization's Edge management channel, add the approved Microsoft Edge
Add-ons listing to the pilot policy. For Windows policy deployment, use the
Microsoft Edge policy template and `ExtensionInstallForcelist` with this entry:

```text
<APPROVED_EDGE_EXTENSION_ID>;https://edge.microsoft.com/extensionwebstorebase/v1/crx
```

The policy accepts a store ID and optional update URL; for a store listing, prefer
the Edge Add-ons service. The precise update endpoint must be checked against the
approved item's current publisher record before use. Do not copy the Chrome ID or
Chrome update URL into Edge policy. Verify installation and policy readback at
`edge://policy` and `edge://extensions`. Microsoft's [ExtensionInstallForcelist
policy reference](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/extensioninstallforcelist)
documents supported update URL behavior and managed installation.

## User-facing behavior and support

The browser displays its own debugger-access notice when Chrome or Edge reads a
PDF response. This notice is expected by the current design; cancelling it stops
the transfer. Do not instruct staff to bypass browser warnings, access challenges,
or KAD rate limits. The visible candidate inventory may be incomplete; the helper
shows that limitation and still requires an employee to choose a candidate. Stop
and escalate when a candidate is missing or changes, or when an access challenge,
acquisition failure, or unknown receipt prevents a safe handoff.

The extension does not read KAD login cookies, passwords, or KAD page auth storage.
The office service page uses its own `sessionStorage` for a pending request UUID
and resume flags; it stores no PDF bytes. The selected-document response chain can
include transient HTML for handoff and challenge checks, but only an accepted PDF
and its source provenance are sent to the authenticated office HTTPS service.
Task source links, chronology and selection reason are retained for reconciliation.
Office OCR and text parsing stay on the office server. No document content is sent
to cloud AI, and no SROArbitr record is saved by the helper.
Support staff should collect only the extension version, browser version, error
code, request/task reference, and non-sensitive timestamps. Never request a PDF,
OCR text, cookie, password, or real case identifier in external support channels.

## Rollback and updates

For a pilot rollback, remove the extension from the pilot force-install policy and
confirm policy readback. Do not remove evidence of an uncertain PDF receipt or
retry the same acquisition until the office task state has been reconciled.
Normal Chrome Web Store and Edge Add-ons installations receive store-managed
updates. Record each reviewed version and package hash before widening rollout;
do not deploy an unreviewed source build.

## Enterprise references

- [Chrome force installation for managed organizations](https://support.google.com/chrome/a/answer/6306504)
- [Chrome extension update lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/extensions-update-lifecycle)
- [Edge ExtensionInstallForcelist policy](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/extensioninstallforcelist)
- [Edge extension publication](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/publish-extension)
