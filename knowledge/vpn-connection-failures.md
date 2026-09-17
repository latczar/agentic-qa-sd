# VPN client fails to connect after password change

Users who have recently changed their network password often find the VPN client
rejects them with "authentication failed", even though the new password works
fine for email and the intranet.

The VPN client caches the old credentials in the Windows Credential Manager. It
keeps presenting the cached password until that entry is cleared.

Resolution steps:

1. Open Credential Manager and delete any saved entry beginning with `vpn.`.
2. Quit the VPN client fully from the system tray, then reopen it.
3. Sign in with the new password and tick "remember me" once it succeeds.

If authentication still fails after clearing the cached entry, the account may
be locked out at the directory level rather than having a password problem.
Check the account lockout status before escalating to the network team.
