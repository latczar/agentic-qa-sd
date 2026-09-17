# Account keeps locking out every few minutes

A repeatedly locking account almost always means a stale password is being
retried automatically somewhere, not that someone is guessing it.

The usual culprits, in order of how often they turn out to be the cause:

1. A phone still configured with the old mailbox password, retrying every sync.
2. A mapped network drive saved with old credentials.
3. A scheduled task or service on a desktop running as the user.
4. A second signed-in session on a machine the user has forgotten about.

Check the lockout source in the directory logs before changing anything. The
event records which machine presented the bad password, which turns this from
guesswork into a single targeted fix.

Resetting the password again without finding the source will not help - the
account will simply lock again on the next retry.
