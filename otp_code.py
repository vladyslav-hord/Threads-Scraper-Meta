"""Print a current TOTP code without placing the secret in command history."""
import getpass
import pyotp


def main() -> None:
    secret = "".join(getpass.getpass("TOTP secret: ").split())
    print(pyotp.TOTP(secret).now())


if __name__ == "__main__":
    main()
