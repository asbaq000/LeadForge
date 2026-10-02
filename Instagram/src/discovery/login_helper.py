"""
Fallback login when `instaloader --login=<user>` hangs.

That hang is a known getpass issue: Python's hidden-password prompt needs a
real console handle, and some terminal setups (certain PowerShell hosts,
IDE-integrated terminals) don't give it one — the prompt just sits there with
no feedback and no way to type.

This does the same login instaloader's own CLI does, just with input() for
the password instead of getpass(). The one real difference: the password
IS visible on screen as you type it here, and stays in your terminal's
scrollback until you clear it or close the window. If that's a problem for
your setup, closing the terminal afterward (or running `cls`) clears it.

Nothing here is sent anywhere but Instagram's own login endpoint, and this
script never stores or prints the password anywhere after login completes.

Usage:
    python src/discovery/login_helper.py your_throwaway_username
"""

import sys

import instaloader


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        sys.exit("Usage: python src/discovery/login_helper.py <instagram_username>")
    username = argv[0]

    password = input(f"Instagram password for {username} (visible as you type): ")

    L = instaloader.Instaloader()
    try:
        L.login(username, password)
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        code = input("Two-factor code: ")
        try:
            L.two_factor_login(code)
        except instaloader.exceptions.BadCredentialsException as exc:
            sys.exit(f"2FA code rejected: {exc}")
    except instaloader.exceptions.BadCredentialsException:
        sys.exit("Login failed: incorrect username or password.")
    except instaloader.exceptions.ConnectionException as exc:
        sys.exit(f"Login failed: {exc}")

    L.save_session_to_file()
    print(f"\nLogged in and saved a session for {username}.")
    print(f"You can now run:\n"
          f"    python src/find_creators.py -k data/keywords.txt --login {username}")


if __name__ == "__main__":
    main()
