"""iothing-auth: one user database for the ioThing apps.

Every app (ioPix, ioNotes, ioContacts) signs its users in against the same MariaDB database (the Shared ioSuite database, `iosuite` by default), so one
account, one password and one session serve the whole suite: signed in at photo.iothing.net is signed in at
notes.iothing.net. What differs per app is kept per app: the role an account holds there (viewer, editor, admin, or
none), its settings there, and its app passwords.

  from iothing_auth import users, sessions, roles, settings, proxyauth   # the parts
  from iothing_auth.fastapi import Auth                                  # the FastAPI glue (middleware, access, routes)

Configuration (environment): IOSUITE_SHARED_DB_HOST, _PORT, _NAME, _USER, _PASSWORD (the database; the older
IOTHING_AUTH_DB_* names still work), and
IOTHING_COOKIE_DOMAIN (the domain the session cookie is shared on, e.g. iothing.net; a request from a host outside it,
such as a LAN address, gets a cookie for that host only).
"""
__version__ = "0.4.0"
