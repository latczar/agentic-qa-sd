# Application times out or hangs when saving

Timeouts when saving are usually network or session related rather than a fault
in the application itself.

If the application was left open overnight, the session has likely expired while
the window still looks signed in. Signing out fully and back in resolves this
and is worth trying first. Copy unsaved work elsewhere before doing so - a stale
session often cannot save what is on screen.

Timeouts only over VPN, and never in the office, point at the connection rather
than the application.

If several people report timeouts on the same application within a short window,
it is a service issue rather than an individual fault. Check the service status
page before raising separate tickets.
