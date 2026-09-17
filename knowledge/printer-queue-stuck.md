# Print jobs stuck in the queue

Jobs that sit in the queue without printing, and cannot be cancelled, mean the
print spooler has jammed on a malformed job.

Resolution:

1. Stop the Print Spooler service.
2. Delete everything in the spooler's PRINTERS folder.
3. Start the Print Spooler service again.
4. Reprint one short document to confirm the queue flows.

Clearing the folder discards every queued job for that machine, including other
users' jobs on a shared machine. Say so before doing it rather than after.

A queue that jams again within the same day usually points at a specific
document or a failing driver rather than the spooler itself.
