# New starter is missing expected access on day one

New accounts are provisioned from the HR record, and access follows the job role
attached to that record. Missing access on day one usually means the role was
blank or wrong when provisioning ran, not that provisioning failed.

Check in this order:

1. Does the account exist and can the user sign in at all? If not, this is a
   provisioning failure, not an access one.
2. Does the HR record show the correct job role and start date?
3. Were any systems requested that fall outside the standard role bundle? Those
   are never automatic and always need a separate request.

Correcting the HR role triggers a fresh provisioning run within an hour. Manual
group additions are a last resort for genuine day-one urgency, and must be
recorded on the ticket so they are not mistaken for role-derived access later.
