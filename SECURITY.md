# Security and data handling

## What this repository excludes

Production Telegram tokens, Google/OAuth keys, personal Telegram IDs, employee
records, maintenance histories, attendance records, equipment inventories, logs,
backups, photographs, documents and local environment files are not part of this snapshot.
Configuration examples contain no working values. Operational dictionaries start empty.

Do not add these materials to commits, issue reports or screenshots. A private
repository and `.gitignore` are not substitutes for removing secrets from source.

## Important limitations

- **Onboarding needs review.** Operator and foreman deep links can register users;
  an operator using the foreman link can be upgraded. Administrator notifications
  are not approval gates. Links are not one-time credentials. Do not distribute
  these links in an untrusted environment without changing that policy.
- **Permissions need workflow tests.** Role-specific menus do not by themselves
  establish that every callback is sufficiently authorized.
- **External writes are real.** When configured and started, the application can
  send Telegram messages and modify Google Sheets. Some reporting operations clear
  or rebuild worksheet contents. Use a dedicated test spreadsheet.
- **Retries are not transactions.** Repeating a write after a connection error can
  duplicate work. Local state and Google Sheets can temporarily disagree.
- **Runtime files contain personal data.** Protect JSON files, logs and backups
  with suitable local access restrictions and an appropriate retention policy.
- **Dependency and end-to-end security audits have not been performed.** The
  supplied tests are limited offline checks, not a guarantee of secret detection
  or application security.

## Safe development

1. Use a new test bot and a blank spreadsheet, never production accounts by default.
2. Store credentials outside the repository and supply paths via environment variables.
3. Inspect `git diff --cached` and the complete tracked file list before each push.
4. Use invented names and equipment in examples; do not reuse real file IDs or screenshots.
5. If a credential is ever committed, revoke/rotate it immediately. Deleting the
   visible file does not remove it from previous commits or invalidate the credential.

Report suspected leaks or vulnerabilities privately to the repository owner through
an already established private channel; do not include secrets in public issues.
