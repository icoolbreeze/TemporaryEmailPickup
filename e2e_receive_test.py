"""Apply for a mailbox, wait for one message, print it, then destroy the mailbox."""

from __future__ import annotations

import secrets
import string
import sys
import time

from rootsh_client import RootshClient, html_to_text


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    client = RootshClient()
    address = ""
    try:
        client.bootstrap()
        suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        mailbox = client.apply_mailbox(f"codextest{suffix}", "bccto.cc")
        address = mailbox.address
        print(f"ADDRESS={address}", flush=True)
        print(f"LIFETIME={mailbox.lifetime_seconds}", flush=True)

        cursor = 0
        deadline = time.time() + min(mailbox.lifetime_seconds, 240)
        while time.time() < deadline:
            update = client.get_mail(address, cursor)
            cursor = update.cursor
            if update.messages:
                message = update.messages[0]
                body = html_to_text(client.fetch_message_html(address, message.message_id))
                print(f"SENDER={message.sender}", flush=True)
                print(f"SUBJECT={message.subject}", flush=True)
                print(f"RECEIVED_AT={message.received_at}", flush=True)
                print(f"BODY={body[:2000]}", flush=True)
                print("RESULT=PASS", flush=True)
                return
            print("POLL=empty", flush=True)
            time.sleep(5)
        print("RESULT=TIMEOUT", flush=True)
    finally:
        if address:
            try:
                client.destroy_mailbox()
                print("CLEANUP=mailbox_destroyed", flush=True)
            except Exception as exc:
                print(f"CLEANUP=failed:{exc}", flush=True)
        client.close()


if __name__ == "__main__":
    main()
