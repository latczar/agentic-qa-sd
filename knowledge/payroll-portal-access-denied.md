# Payroll portal shows "access denied" after a role change

Staff who have recently changed role or department often lose payroll portal
access. The portal takes its permissions from the HR system rather than from the
directory, and the two synchronise overnight.

What this means in practice:

1. A role change made today will not reach the payroll portal until tomorrow.
2. Granting directory group membership manually has no effect and should not be
   attempted - it will be overwritten at the next sync.
3. Access denied on the day of a role change is expected behaviour.

If access is still denied more than one full working day after the HR record was
updated, the sync itself has failed and the ticket should go to the HR systems
team, not the identity team.
