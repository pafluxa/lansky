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


def run_once() -> int:
    processed = 0
    criteria = AND(seen=False, from_=config.BCI_SENDER)

    with MailBox(config.IMAP_HOST).login(config.IMAP_USER, config.IMAP_PASSWORD) as mailbox:
        mailbox.folder.set(config.IMAP_FOLDER)

        for msg in mailbox.fetch(criteria):
            html_body = msg.html or msg.text
            if not html_body:
                log.warning("No body in message: %s", msg.subject)
                continue

            text = preprocessor.preprocess(html_body)

            result = llm_client.extract(text)
            if result is None:
                log.error("Extraction failed for: %s — will retry next cycle", msg.subject)
                continue

            all_ok = True
            for transaction in result.transactions:
                resp = pusher.push(transaction)
                if resp is None:
                    log.error("Push failed for transaction in: %s", msg.subject)
                    all_ok = False

            if all_ok:
                mailbox.flag(msg.uid, MailMessageFlags.SEEN, True)
                log.info("Processed: %s → %d transaction(s)", msg.subject, len(result.transactions))
                processed += 1
            else:
                log.warning("Skipping seen-mark for %s due to push failure", msg.subject)

    return processed


def main() -> None:
    log.info("Lansky extractor starting. Polling every %ds.", config.POLL_INTERVAL_SECONDS)
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
