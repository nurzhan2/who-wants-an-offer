"""Job-alert letters in the owner's own mailbox: LinkedIn, Indeed, Glassdoor, hh.

The legal way to LinkedIn and Indeed (phase 22). Their terms forbid automated
collection, ``BLOCKED_HOSTS`` refuses their hosts at the transport, and nothing
here changes that: the owner subscribes to these services' Job Alerts with
their own address, the services send the letters, and this source reads **only
those letters** from the owner's mailbox.

**What it does not do, by construction.**

* It never opens a linkedin.com, indeed.com or glassdoor.com page — not even
  the one a letter links to. There is no HTTP request in this module at all.
  A vacancy keeps what the letter says (title, company, location, link); its
  description arrives when jsearch or an ATS board brings the same posting and
  the shared fingerprint merges the two rows.
* It does not store letters. Each posting keeps the extracted fields, the
  sender's key and the letter's ``Message-ID``; the body is parsed in memory
  and dropped. The job link is rebuilt from the job id, so the tracking
  parameters in the letter — which identify the owner — are not kept either.
* It does not write to the mailbox: the folder is ``SELECT``-ed read-only and
  messages are fetched with ``BODY.PEEK[]``, so nothing is marked read, moved,
  flagged or answered.
* It reads nothing from senders outside ``email_alert_senders.yaml``: the IMAP
  search is built from that list, so a person's letter is never downloaded.

**City.** Left empty on purpose and the location kept as text in
``_derived.location``. The fingerprint includes the city, and jsearch — the
source most likely to carry the same LinkedIn posting with a description —
states none; a city here would give the two rows different fingerprints and the
description would never arrive.
"""

import asyncio
import email
import hashlib
import imaplib
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar, Protocol

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.core.exceptions import ConfigurationError, SourceError
from app.core.logging import get_logger
from app.sources.base import (
    AccessMode,
    BaseSource,
    RateLimit,
    RawPosting,
    SearchQuery,
    SourceUnavailable,
    Unavailable,
)
from app.sources.registry import register_source

logger = get_logger(__name__)

SENDERS_FILE = Path(__file__).with_name("email_alert_senders.yaml")
STATE_SENDER = "sender:"
STATE_RUN = "run:last"
#: Letters read per sender per run, newest last. A daily alert is one letter.
MAX_LETTERS = 50
MAX_TITLE = 300
MAX_COMPANY = 200


class Sender(BaseModel):
    """One whitelisted sender and how its letters name a job."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    addresses: tuple[str, ...] = Field(alias="from", min_length=1)
    links: tuple[str, ...] = Field(min_length=1)
    canonical: str = Field(pattern=r"\{id\}")

    def patterns(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(link, re.IGNORECASE) for link in self.links)


def load_senders(path: Path | None = None) -> tuple[Sender, ...]:
    """The whitelist. A broken file is a configuration error, loudly."""
    target = path or SENDERS_FILE
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        senders = tuple(Sender.model_validate(entry) for entry in document.get("senders", []))
        for sender in senders:
            sender.patterns()
        return senders
    except (OSError, yaml.YAMLError, ValidationError, AttributeError, re.error) as exc:
        raise ConfigurationError(f"{target.name} не читается: {exc}") from exc


# ── reading one letter ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AlertJob:
    """One job as a letter presents it."""

    job_id: str
    url: str
    title: str
    company: str | None
    location: str | None


class _Tokens(HTMLParser):
    """The letter as a flat run of text pieces, each with the link it sits in."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[str, str | None]] = []
        self._href: list[str | None] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"style", "script", "head", "title"}:
            self._skip += 1
        if tag == "a":
            self._href.append(dict(attrs).get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "head", "title"} and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self._href.pop()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and not self._skip:
            self.tokens.append((text, self._href[-1] if self._href else None))


#: Text pieces that are buttons or boilerplate, never a company or a place.
_NOISE = re.compile(
    r"^(view job|apply|apply now|easy apply|see all jobs|unsubscribe|new|promoted|"
    r"откликнуться|подробнее|смотреть|новая|actively recruiting|"
    r"\d+ (?:applicants|connections?).*)$",
    re.IGNORECASE,
)


def parse_alert(html: str, sender: Sender) -> list[AlertJob]:
    """Every job a letter links to, with the two lines of text after its title.

    A job is an anchor whose ``href`` matches one of the sender's patterns and
    whose text is not a button. Its company and location are the next two text
    pieces that are not links to the same job and not boilerplate — which is
    how all four services lay a card out. Anything less certain stays None.
    """
    parser = _Tokens()
    parser.feed(html)
    tokens = parser.tokens
    found: dict[str, AlertJob] = {}
    for index, (text, href) in enumerate(tokens):
        job_id = _job_id(href, sender)
        if job_id is None or job_id in found or _NOISE.match(text) or len(text) < 3:
            continue
        after: list[str] = []
        for later_text, later_href in tokens[index + 1 : index + 8]:
            if _job_id(later_href, sender) not in {None, job_id}:
                break
            if _NOISE.match(later_text) or later_text == text:
                continue
            after.append(later_text)
            if len(after) == 2:
                break
        company = after[0] if after else None
        location = after[1] if len(after) > 1 else None
        found[job_id] = AlertJob(
            job_id=job_id,
            url=sender.canonical.format(id=job_id),
            title=text[:MAX_TITLE],
            company=company[:MAX_COMPANY] if company else None,
            location=location[:120] if location else None,
        )
    return list(found.values())


def _job_id(href: str | None, sender: Sender) -> str | None:
    if not href:
        return None
    for pattern in sender.patterns():
        match = pattern.search(href)
        if match:
            return match.group(1)
    return None


def html_of(message: Message) -> str:
    """The letter's HTML part, or its text part wrapped so the parser reads it."""
    parts = message.walk() if message.is_multipart() else iter([message])
    text: str | None = None
    for part in parts:
        kind = part.get_content_type()
        if kind not in {"text/html", "text/plain"}:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if kind == "text/html":
            return decoded
        text = text or decoded
    if text is None:
        return ""
    # Plain-text alerts carry bare URLs; turn each into an anchor with the line
    # before it as its text, so one parser reads both shapes.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    html: list[str] = []
    for index, line in enumerate(lines):
        if re.match(r"https?://", line):
            title = lines[index - 1] if index else line
            html.append(f'<a href="{line}">{title}</a>')
        else:
            html.append(f"<p>{line}</p>")
    return "".join(html)


# ── the mailbox ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AlertLetter:
    """One fetched letter, before parsing. Never stored."""

    uid: int
    message_id: str
    received_at: datetime | None
    sender: str
    html: str


class AlertMailbox(Protocol):
    """What the source needs from a mailbox. A fake implements it in tests."""

    async def letters(
        self, addresses: Sequence[str], *, since: datetime, after_uid: int
    ) -> tuple[int, Sequence[AlertLetter]]:
        """(UIDVALIDITY, letters from these senders after ``after_uid``)."""
        ...


class ImapMailbox:
    """The owner's mailbox over IMAPS, opened read-only.

    ``imaplib`` is blocking, so every call runs in a thread. One connection per
    run, logged out at the end.
    """

    def __init__(self, *, host: str, port: int, user: str, password: str, folder: str) -> None:
        self._host, self._port = host, port
        self._user, self._password, self._folder = user, password, folder

    async def letters(
        self, addresses: Sequence[str], *, since: datetime, after_uid: int
    ) -> tuple[int, Sequence[AlertLetter]]:
        return await asyncio.to_thread(self._letters, list(addresses), since, after_uid)

    def _letters(
        self, addresses: list[str], since: datetime, after_uid: int
    ) -> tuple[int, list[AlertLetter]]:
        connection = imaplib.IMAP4_SSL(self._host, self._port)
        try:
            connection.login(self._user, self._password)
            status, _ = connection.select(_quoted(self._folder), readonly=True)
            if status != "OK":
                raise SourceError(f"email_alerts: папка {self._folder!r} не открылась")
            validity = _uidvalidity(connection)
            uids: set[int] = set()
            for address in addresses:
                criteria = f'(FROM "{address}" SINCE {since.strftime("%d-%b-%Y")})'
                status, found = connection.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
                if status == "OK" and found and found[0]:
                    uids.update(int(uid) for uid in found[0].split())
            letters: list[AlertLetter] = []
            for uid in sorted(uid for uid in uids if uid > after_uid)[-MAX_LETTERS:]:
                status, parts = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((item[1] for item in parts or [] if isinstance(item, tuple)), None)
                if status != "OK" or not isinstance(raw, bytes):
                    continue
                message = email.message_from_bytes(raw)
                letters.append(_letter(uid, message))
            return validity, letters
        except imaplib.IMAP4.error as exc:
            raise SourceError(
                f"email_alerts: IMAP отказал ({exc})", source_slug="email_alerts"
            ) from exc
        finally:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError) as exc:
                logger.debug("sources.email_alerts.logout_failed", error=type(exc).__name__)


def _quoted(folder: str) -> str:
    return folder if folder.isalnum() else '"' + folder.replace('"', r"\"") + '"'


def _uidvalidity(connection: imaplib.IMAP4) -> int:
    status, data = connection.response("UIDVALIDITY")
    if status == "OK" and data and data[0]:
        return int(data[0])
    return 0


def _letter(uid: int, message: Message) -> AlertLetter:
    received: datetime | None = None
    if message.get("Date"):
        try:
            received = parsedate_to_datetime(str(message["Date"]))
        except (TypeError, ValueError):
            received = None
    return AlertLetter(
        uid=uid,
        message_id=str(message.get("Message-ID", "")).strip() or f"uid:{uid}",
        received_at=received,
        sender=parseaddr(str(message.get("From", "")))[1].casefold(),
        html=html_of(message),
    )


# ── the source ────────────────────────────────────────────────────────


@register_source
class EmailAlertsSource(BaseSource):
    """Job alerts from LinkedIn, Indeed, Glassdoor and hh, read from the owner's mailbox.

    Terms: the owner's own mailbox, read with the owner's app password over IMAP
    (provider terms, e.g. https://support.google.com/mail/answer/7126229, allow
    third-party IMAP clients the owner configures). The alert senders' sites are
    never contacted: their terms forbid automated collection, and this source
    only reads letters those services chose to send to the owner. Read-only:
    no letter is marked, moved, deleted or answered; only extracted fields and
    the message id are kept. Attribution: the original service, per posting.
    """

    slug: ClassVar[str] = "email_alerts"
    name: ClassVar[str] = "Письма-подписки"
    regions: ClassVar[tuple[str, ...]] = ("world", "KZ", "RU")
    requires_auth: ClassVar[bool] = True
    access_mode: ClassVar[AccessMode] = AccessMode.API
    terms_url: ClassVar[str | None] = "https://support.google.com/mail/answer/7126229"
    attribution: ClassVar[str | None] = (
        "из письма-подписки владельца (LinkedIn, Indeed, Glassdoor, hh)"
    )
    rate_limit: ClassVar[RateLimit] = RateLimit(requests_per_second=1.0)
    min_interval: ClassVar[timedelta] = timedelta(hours=6)

    def __init__(
        self,
        *,
        mailbox: AlertMailbox | None = None,
        senders: Sequence[Sender] | None = None,
        now: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._mailbox = mailbox
        self._senders = tuple(senders) if senders is not None else None
        self._now = now or (lambda: datetime.now(UTC))
        self._yielded = 0
        self._pending: list[tuple[int, str, dict[str, Any]]] = []

    def unavailable(self) -> Unavailable | None:
        if self._mailbox is not None:
            return None
        missing = tuple(
            name
            for name, value in (
                ("EMAIL_ALERTS_IMAP_USER", settings.email_alerts_imap_user),
                ("EMAIL_ALERTS_IMAP_PASSWORD", settings.email_alerts_imap_password),
            )
            if not value
        )
        if missing:
            return Unavailable(
                code=SourceUnavailable.MISSING_CREDENTIALS,
                detail=(
                    f"Письма-подписки не настроены: не заданы {', '.join(missing)} в .env "
                    "(пароль приложения, не пароль от почты). Шаги — README, раздел «Почта»."
                ),
                missing_credentials=missing,
            )
        return None

    def _make_mailbox(self) -> AlertMailbox:
        if self._mailbox is not None:
            return self._mailbox
        user, password = settings.email_alerts_imap_user, settings.email_alerts_imap_password
        if user is None or password is None:  # pragma: no cover - unavailable()
            raise ConfigurationError("почта не настроена")
        return ImapMailbox(
            host=settings.email_alerts_imap_host,
            port=settings.email_alerts_imap_port,
            user=user,
            password=password.get_secret_value(),
            folder=settings.email_alerts_folder,
        )

    def search(self, query: SearchQuery) -> AsyncIterator[RawPosting]:
        return self.search_batch([query])

    async def search_batch(self, queries: Sequence[SearchQuery]) -> AsyncIterator[RawPosting]:
        """Every job in the new alert letters. The owner chose these alerts; no filter."""
        mailbox = self._make_mailbox()
        senders = self._senders if self._senders is not None else load_senders()
        self._yielded, self._pending = 0, []
        report: dict[str, Any] = {"letters": 0, "jobs": 0, "letters_without_jobs": 0, "by": {}}
        try:
            for sender in senders:
                key = f"{STATE_SENDER}{sender.key}"
                stored = await self.state_get(key) or {}
                since = self._now() - timedelta(days=settings.email_alerts_since_days)
                validity, letters = await mailbox.letters(
                    sender.addresses,
                    since=since,
                    after_uid=int(stored.get("last_uid") or 0) if stored.get("uidvalidity") else 0,
                )
                if stored.get("uidvalidity") and stored["uidvalidity"] != validity:
                    # The folder was rebuilt: UIDs mean something else now. The
                    # since-window bounds what is re-read; upserts are idempotent.
                    logger.info("sources.email_alerts.uidvalidity_changed", sender=sender.key)
                seen: set[str] = set()
                last_uid = int(stored.get("last_uid") or 0)
                for letter in letters:
                    report["letters"] += 1
                    jobs = parse_alert(letter.html, sender)
                    if not jobs:
                        report["letters_without_jobs"] += 1
                        logger.warning(
                            "sources.email_alerts.no_jobs",
                            sender=sender.key,
                            message=_digest(letter.message_id),
                        )
                    for job in jobs:
                        if job.job_id in seen:
                            continue
                        seen.add(job.job_id)
                        report["jobs"] += 1
                        report["by"][sender.key] = report["by"].get(sender.key, 0) + 1
                        self._yielded += 1
                        yield self._posting(sender, job, letter)
                    last_uid = max(last_uid, letter.uid)
                self._pending.append(
                    (self._yielded, key, {"uidvalidity": validity, "last_uid": last_uid})
                )
        finally:
            report["finished_at"] = self._now().isoformat()
            await self.state_set(STATE_RUN, report)
            logger.info(
                "sources.email_alerts.run_finished",
                **{k: v for k, v in report.items() if k != "by"},
                by=report["by"],
            )

    async def record_progress(self, durable: int) -> None:
        still = []
        for needed, key, value in self._pending:
            if needed <= durable:
                await self.state_set(key, value)
            else:
                still.append((needed, key, value))
        self._pending = still

    def _posting(self, sender: Sender, job: AlertJob, letter: AlertLetter) -> RawPosting:
        return RawPosting(
            source_slug=self.slug,
            external_id=f"{sender.key}:{job.job_id}",
            url=job.url,
            title=job.title,
            company=job.company,
            description=None,
            raw={
                "_derived": {
                    "location": job.location,
                    "remote": "full" if job.location and _says_remote(job.location) else None,
                    "via": sender.key,
                },
                # The letter itself is not kept: its id, to say where the
                # posting came from, and when it arrived.
                "email": {
                    "message_id": letter.message_id,
                    "received_at": letter.received_at.isoformat() if letter.received_at else None,
                },
            },
        )

    def publisher_of(self, raw: dict[str, Any]) -> str | None:
        """«письмо → LinkedIn» on the card."""
        derived = raw.get("_derived")
        via = derived.get("via") if isinstance(derived, dict) else None
        names = {"linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor", "hh": "hh"}
        return names.get(via, via) if isinstance(via, str) else None


def _says_remote(location: str) -> bool:
    return bool(re.search(r"remote|удал[её]нн|удалёнка|удаленка", location, re.IGNORECASE))


def _digest(message_id: str) -> str:
    """A short stable handle for a letter in a log line, not the id itself."""
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:12]
