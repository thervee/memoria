"""Set (or change) the Memoria dashboard password.

Only a scrambled fingerprint (scrypt hash) is saved, in keys/dashboard_password.json,
which is git-ignored and readable only by your Windows account.
Double-click set_dashboard_password.bat to run this.
"""
import getpass
import sys

import dashboard_password

print("Choose a password for the Memoria dashboard (at least "
      f"{dashboard_password.MIN_PASSWORD_LENGTH} characters).")
print("Nothing appears on screen while you type. That's normal.\n")
first = getpass.getpass("New password: ")
second = getpass.getpass("Type it again: ")
if first != second:
    sys.exit("\nThe two passwords didn't match. Nothing was changed; please try again.")
try:
    dashboard_password.set_password(first)
except ValueError as e:
    sys.exit(f"\n{e} Nothing was changed.")
print("\nPassword saved. If Memoria is running, restart it (stop_memoria.bat, then")
print("start_memoria.bat) so anyone logged in with the old password is logged out.")
