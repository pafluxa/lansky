import logging
import time

from imap_tools import AND, MailBox, MailMessageFlags

import config
import llm_client
import preprocessor
import pusher

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
)
log = logging.getLogger(__name__)


def _process_message(msg, mailbox) -> bool:
    """Process a single email. Returns True on success."""
    html_body = msg.html or msg.text
    if not html_body:
        log.warning("No body in message: %s", msg.subject)
        return False

    text = preprocessor.preprocess(html_body)
    bank_name = preprocessor.detect_bank(html_body)
    result = llm_client.extract(text, bank_name=bank_name)
    if result is None:
        log.error("Extraction failed for: %s — will retry next cycle", msg.subject)
        return False

    all_ok = True
    for transaction in result.transactions:
        resp = pusher.push(transaction)
        if resp is None:
            log.error("Push failed for transaction in: %s", msg.subject)
            all_ok = False

    if all_ok:
        mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
        log.info("Processed: %s → %d transaction(s)", msg.subject, len(result.transactions))

    return all_ok


def run_once() -> int:
    processed = 0
    with MailBox(config.IMAP_HOST).login(config.IMAP_USER, config.IMAP_PASSWORD) as mailbox:
        mailbox.folder.set(config.IMAP_FOLDER)

        for sender in config.BANK_SENDERS:
            criteria = AND(seen=False, from_=sender)
            for msg in mailbox.fetch(criteria):
                if _process_message(msg, mailbox):
                    processed += 1
    return processed


def main() -> None:
    log.info("Lansky extractor starting. Polling every %ds.", config.POLL_INTERVAL_SECONDS)
    if not config.BANK_SENDERS:
        log.error("No BANK_SENDERS configured. Set the BANK_SENDERS env var.")
        return
    try:
        while True:
            count = run_once()
            log.info(
                "Cycle complete. Processed %d email(s). Sleeping %ds.",
                count,
                config.POLL_INTERVAL_SECONDS,
            )
            time.sleep(config.POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
