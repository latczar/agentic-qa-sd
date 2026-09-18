# Browser shows a certificate warning on an internal site

A certificate warning on an internal site usually means one of three things:
the certificate has expired, the device is missing the internal root
certificate, or the site is being reached over a network that intercepts
traffic.

Check the date on the warning. An expired certificate affects everyone at once
and is almost certainly already known.

If the warning appears only on your device, the internal root certificate is
likely missing, which happens on a freshly rebuilt machine before all policies
have applied. Restarting while connected to the corporate network usually pulls
it down.

Never click through a certificate warning on a site where you will enter
credentials. The warning is the only signal that something sits between you and
the service.
