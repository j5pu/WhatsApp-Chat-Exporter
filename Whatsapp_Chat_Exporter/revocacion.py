"""Revocación fork: TXT output with the same shape as a WhatsApp ``_chat.txt``.

Enabled with ``--whatsapp-format`` (iOS only, together with ``--txt``).

* One line per message: ``[dd/mm/yyyy, hh:mm:ss] Sender: text`` in local time
  (with daylight saving), continuation lines unprefixed, own messages as ``Me``.
* One file per chat, never overwritten: ``Name [+phone].txt`` for people,
  ``Group name [id@g.us].txt`` for groups, ``+phone.txt`` without a name.
* Group senders written as ``@lid`` are resolved to their phone and name with
  the ``ZLID`` column of ContactsV2 (and WhatsApp push names as last resort).
* Attachments are not copied: each one is written as
  ``<attached: path-in-backup (original name)>`` where the path is the file
  name inside the iPhone backup (``xx/<sha1>``, or ``<sha1>`` for flat iOS <= 9
  backups), computed as SHA-1 of ``<domain>-Message/<ZMEDIALOCALPATH>``.
* Quoted replies get a ``Responde a: [date] Sender`` line.

Everything the exporter reads comes from the databases; the databases are only
read (``mode=ro&immutable=1`` for the extra lookups done here).
"""

import hashlib
import math
import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, Optional, Tuple

ME = "Me"
CALLS_KEY = "000000000000000"


def _ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    con.text_factory = lambda b: b.decode(encoding="utf-8", errors="replace")
    return con


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _columns(con: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def stamp(ts: float) -> str:
    # Whole seconds of ZMESSAGEDATE (fraction truncated). Checked against an
    # official WhatsApp export of the same chat: messages sent from this iPhone
    # can differ by 1 second there, because WhatsApp rounds the fraction its own way.
    return datetime.fromtimestamp(math.floor(ts)).strftime("%d/%m/%Y, %H:%M:%S")


def phone_of(jid: Optional[str]) -> Optional[str]:
    if jid and jid.endswith("@s.whatsapp.net"):
        return "+" + jid.split("@")[0]
    return None


class Directory:
    """Names and LID -> phone map from ContactsV2 and ChatStorage."""

    def __init__(self, msg_db: str, contact_db: Optional[str]):
        self.name_by_jid: Dict[str, str] = {}
        self.phone_jid_by_lid: Dict[str, str] = {}
        self.push_by_jid: Dict[str, str] = {}
        if contact_db and os.path.isfile(contact_db):
            con = _ro(contact_db)
            if _has_table(con, "ZWAADDRESSBOOKCONTACT"):
                cols = _columns(con, "ZWAADDRESSBOOKCONTACT")
                lid = "ZLID" if "ZLID" in cols else "NULL"
                for row in con.execute(f"SELECT ZWHATSAPPID, ZFULLNAME, {lid} AS ZLID FROM ZWAADDRESSBOOKCONTACT"):
                    jid = row["ZWHATSAPPID"]
                    if jid and "@" not in jid:
                        jid += "@s.whatsapp.net"
                    if jid and row["ZFULLNAME"]:
                        self.name_by_jid.setdefault(jid, row["ZFULLNAME"])
                    if jid and row["ZLID"]:
                        self.phone_jid_by_lid.setdefault(row["ZLID"], jid)
            elif _has_table(con, "ZWAPHONE") and _has_table(con, "ZWACONTACT"):
                # Old WhatsApp (iOS 7) Contacts.sqlite
                for row in con.execute("""SELECT p.ZWHATSAPPID, c.ZFULLNAME FROM ZWAPHONE p
                                          JOIN ZWACONTACT c ON p.ZCONTACT = c.Z_PK"""):
                    jid = row["ZWHATSAPPID"]
                    if jid and "@" not in jid:
                        jid += "@s.whatsapp.net"
                    if jid and row["ZFULLNAME"]:
                        self.name_by_jid.setdefault(jid, row["ZFULLNAME"])
            con.close()
        con = _ro(msg_db)
        if _has_table(con, "ZWAPROFILEPUSHNAME"):
            for row in con.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
                if row["ZJID"] and row["ZPUSHNAME"]:
                    self.push_by_jid[row["ZJID"]] = row["ZPUSHNAME"]
        con.close()

    def resolve(self, jid: Optional[str]) -> Optional[str]:
        """Phone JID for a LID when known, otherwise the JID itself."""
        if jid and jid.endswith("@lid"):
            return self.phone_jid_by_lid.get(jid, jid)
        return jid


_NUMBER_CHARS = str.maketrans("", "", "+-()" + "".join(map(chr, (
    0x20, 0xA0, 0x2009, 0x202F, 0x2010, 0x2011, 0x2012, 0x2013,
    0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069))))
# Media message types that should carry a file (image, video, audio, document, GIF, sticker...)
MEDIA_TYPES = {1, 2, 3, 8, 11, 13, 15, 20, 54}


def number_like(name: Optional[str]) -> bool:
    """True for names that are only a formatted phone number (e.g. '‪+34 600 36 25 81‬')."""
    return bool(name) and name.translate(_NUMBER_CHARS).isdigit()


def _display(jid: Optional[str], directory: Directory, data) -> Optional[str]:
    """Name for a sender JID: address book, WhatsApp chat name, phone, push name, raw JID."""
    jid = directory.resolve(jid)
    if not jid:
        return None
    name = directory.name_by_jid.get(jid)
    if not name and jid in data and data.get_chat(jid).name:
        name = data.get_chat(jid).name
    if name and not number_like(name):
        return name
    phone = phone_of(jid)
    if phone:
        return phone
    push = directory.push_by_jid.get(jid)
    if push:
        return f"~{push}"
    return jid


def file_stem(jik: str, chat, directory: Directory) -> str:
    if jik == CALLS_KEY:
        return "WhatsApp Calls"
    resolved = directory.resolve(jik)
    name = (chat.name if chat is not None else None) or directory.name_by_jid.get(resolved)
    if number_like(name):
        name = None
    if resolved.endswith("@s.whatsapp.net"):
        ident = phone_of(resolved)
        if jik.endswith("@lid"):
            # A chat stored under the LID is a different chat session from the one
            # stored under the phone number: keep both, never overwrite.
            ident = f"{ident}, {jik}"
    else:
        ident = resolved
    stem = f"{name} [{ident}]" if name else ident
    return stem.replace("/", "-").replace("\\", "-").replace(":", "-")


class MediaRef:
    """Location of a WhatsApp attachment inside the iPhone backup."""

    def __init__(self, domain: str, flat: bool, prefix: str = "Message"):
        self.domain = domain
        self.flat = flat
        self.prefix = prefix.strip("/")

    def __call__(self, local_path: str) -> str:
        # Modern WhatsApp: ZMEDIALOCALPATH 'Media/...' lives at 'Message/Media/...';
        # iOS 7 WhatsApp: '/Media/...' lives at 'Library/Media/...'.
        path = local_path.lstrip("/")
        rel = path if path.startswith(self.prefix + "/") else f"{self.prefix}/{path}"
        file_id = hashlib.sha1(f"{self.domain}-{rel}".encode("utf-8")).hexdigest()
        return file_id if self.flat else f"{file_id[:2]}/{file_id}"


def _extra(msg_db: str) -> Dict[int, sqlite3.Row]:
    """Per message PK: sender JID, media path, coordinates, vCard names."""
    con = _ro(msg_db)
    mcols = _columns(con, "ZWAMEDIAITEM")
    vcard = "mi.ZVCARDNAME" if "ZVCARDNAME" in mcols else "NULL"
    rows = con.execute(f"""
        SELECT m.Z_PK, m.ZMESSAGETYPE, gm.ZMEMBERJID, mi.ZMEDIALOCALPATH, mi.ZTITLE,
               mi.ZLATITUDE, mi.ZLONGITUDE, {vcard} AS ZVCARDNAME, mi.Z_PK AS MEDIA_PK
        FROM ZWAMESSAGE m
            LEFT JOIN ZWAGROUPMEMBER gm ON m.ZGROUPMEMBER = gm.Z_PK
            LEFT JOIN ZWAMEDIAITEM mi ON mi.ZMESSAGE = m.Z_PK
    """).fetchall()
    con.close()
    return {row["Z_PK"]: row for row in rows}


def _body(message, extra: Optional[sqlite3.Row], ref: MediaRef) -> Tuple[Optional[str], str]:
    """Return (text, kind). ``None`` text means the message is not written."""
    mtype = extra["ZMESSAGETYPE"] if extra is not None else message.message_type
    caption = message.caption.replace("<br>", "\n") if message.caption else None
    if extra is not None and extra["ZMEDIALOCALPATH"]:
        path = extra["ZMEDIALOCALPATH"]
        line = f"<attached: {ref(path)} ({os.path.basename(path)})>"
        return (line + ("\n" + caption if caption else "")), "media"
    if mtype == 4 and extra is not None and extra["ZVCARDNAME"]:
        names = extra["ZVCARDNAME"].split("_$!<Name-Separator>!$_")
        return f"<contacto compartido: {', '.join(n for n in names if n)}>", "vcard"
    if mtype == 5 and extra is not None and extra["ZLATITUDE"] is not None:
        return f"<ubicación: {extra['ZLATITUDE']}, {extra['ZLONGITUDE']}>", "location"
    if (message.media and message.data == "The media is missing") or (
            mtype in MEDIA_TYPES and extra is not None and extra["MEDIA_PK"] is not None):
        return ("<attached: sin ruta en la base>" + ("\n" + caption if caption else "")), "media-no-path"
    if mtype == 14:
        # Same wording as WhatsApp's own export
        return ("You deleted this message." if message.from_me else "This message was deleted."), "deleted"
    data = message.data
    if data is None:
        if mtype == 6:
            return None, "system-skipped"
        return f"<mensaje de WhatsApp de tipo {mtype} sin texto>", "empty"
    text = str(data).replace("<br>", "\n")
    if caption:
        text += "\n" + caption
    return text, "text"


def _calls(call_db: Optional[str], directory: Directory):
    """Calls from CallHistory: (timestamp, chat JID, from_me, text-builder data)."""
    if not call_db or not os.path.isfile(call_db):
        return []
    con = _ro(call_db)
    if not _has_table(con, "ZWACDCALLEVENT"):
        con.close()
        return []
    parts: Dict[int, list] = {}
    if _has_table(con, "ZWACDCALLEVENTPARTICIPANT"):
        for row in con.execute("SELECT Z1PARTICIPANTS, ZJIDSTRING FROM ZWACDCALLEVENTPARTICIPANT"):
            if row["ZJIDSTRING"]:
                parts.setdefault(row["Z1PARTICIPANTS"], []).append(row["ZJIDSTRING"])
    calls = []
    for row in con.execute("""
        SELECT c.Z_PK, c.ZDATE, c.ZOUTCOME, c.ZDURATION,
               ifnull(c.ZBYTESRECEIVED, 0) + ifnull(c.ZBYTESSENT, 0) AS bytes_transferred,
               c.ZGROUPJIDSTRING, c.ZGROUPCALLCREATORUSERJIDSTRING, a.ZINCOMING, a.ZVIDEO, a.Z_PK AS AGG_PK
        FROM ZWACDCALLEVENT c JOIN ZWAAGGREGATECALLEVENT a ON c.Z1CALLEVENTS = a.Z_PK
    """):
        # Participants hang from the aggregate call event (Z1PARTICIPANTS = ZWAAGGREGATECALLEVENT.Z_PK)
        people = [directory.resolve(j) for j in parts.get(row["AGG_PK"], [])]
        creator = directory.resolve(row["ZGROUPCALLCREATORUSERJIDSTRING"])
        target = row["ZGROUPJIDSTRING"] or (people[0] if people else creator)
        calls.append({"ts": 978307200 + int(row["ZDATE"]), "chat": target, "from_me": row["ZINCOMING"] == 0,
                      "video": row["ZVIDEO"] == 1, "group": row["ZGROUPJIDSTRING"] is not None,
                      "outcome": row["ZOUTCOME"], "duration": row["ZDURATION"],
                      "bytes": row["bytes_transferred"], "other": people[0] if people else creator,
                      "creator": creator})
    con.close()
    return calls


def _call_text(call, other_name: str) -> str:
    """Same wording as the upstream exporter (format_call_data), with the real other party."""
    from Whatsapp_Chat_Exporter.utility import bytes_to_readable, convert_time_unit
    text = (f"A {'group ' if call['group'] else ''}{'video' if call['video'] else 'voice'} "
            f"call {'to' if call['from_me'] else 'from'} {other_name} was ")
    if call["outcome"] in (1, 4):
        text += "not answered." if call["from_me"] else "missed."
    elif call["outcome"] == 2:
        text += "failed."
    elif call["outcome"] == 0:
        text += (f"initiated and lasted for {convert_time_unit(int(call['duration'] or 0))} "
                 f"with {bytes_to_readable(call['bytes'] or 0)} data transferred.")
    else:
        text += "in an unknown state."
    return text


def create_txt_whatsapp(data, output: str, msg_db: str, contact_db: Optional[str],
                        media_domain: str, flat: bool, call_db: Optional[str] = None,
                        media_prefix: str = "Message") -> Dict[str, int]:
    os.makedirs(output, exist_ok=True)
    directory = Directory(msg_db, contact_db)
    extra = _extra(msg_db)
    ref = MediaRef(media_domain, flat, media_prefix)
    stats: Dict[str, int] = {"files": 0, "written": 0}

    # Calls go into the chat of the other party (or of the group), like WhatsApp does.
    chat_of = {}
    for jik in data.keys():
        if jik != CALLS_KEY:
            chat_of.setdefault(directory.resolve(jik), jik)
    calls_by_chat: Dict[str, list] = {}
    for call in _calls(call_db, directory):
        jik = chat_of.get(call["chat"], CALLS_KEY)
        calls_by_chat.setdefault(jik, []).append(call)

    used = set()
    keys = [k for k in data.keys() if k != CALLS_KEY]
    if calls_by_chat.get(CALLS_KEY):
        keys.append(CALLS_KEY)
    for jik in keys:
        chat = data.get_chat(jik) if (jik in data and jik != CALLS_KEY) else None
        has_messages = chat is not None and len(chat) > 0
        if not has_messages and not calls_by_chat.get(jik):
            continue
        stem = file_stem(jik, chat, directory) if jik != CALLS_KEY else "WhatsApp Calls"
        if stem in used:
            raise RuntimeError(f"Duplicate output file name: {stem} ({jik})")
        used.add(stem)
        is_group = jik.endswith("@g.us")
        chat_name = chat.name if chat is not None else None
        partner = _display(jik, directory, data) if not is_group and jik != CALLS_KEY else None
        entries = []  # (timestamp, line)
        if has_messages:
            by_key = {m.key_id: (pk, m) for pk, m in chat.items() if m.key_id}
            for pk, message in chat.items():
                row = extra.get(pk) if isinstance(pk, int) else None
                text, kind = _body(message, row, ref)
                if row is not None and row["ZMESSAGETYPE"] == 59 and any(
                        abs(c["ts"] - message.timestamp) <= 120 for c in calls_by_chat.get(jik, [])):
                    # Call record in the chat: the call itself is written from CallHistory.
                    text, kind = None, "call-message-covered"
                stats[kind] = stats.get(kind, 0) + 1
                if text is None:
                    continue
                if message.from_me:
                    sender = ME
                elif is_group:
                    member = row["ZMEMBERJID"] if row is not None else None
                    # Group notices without a member are written with the group name, as WhatsApp does.
                    sender = _display(member, directory, data) if member else (chat_name or jik)
                else:
                    sender = partner
                if message.reply:
                    found = by_key.get(message.reply)
                    if found is not None:
                        qpk, quoted = found
                        if quoted.from_me:
                            qsender = ME
                        elif is_group:
                            qrow = extra.get(qpk) if isinstance(qpk, int) else None
                            qmember = qrow["ZMEMBERJID"] if qrow is not None else None
                            qsender = _display(qmember, directory, data) if qmember else (chat_name or jik)
                        else:
                            qsender = partner
                        text += f"\nResponde a: [{stamp(quoted.timestamp)}] {qsender}"
                    else:
                        text += "\nResponde a un mensaje que no está en la copia"
                    stats["replies"] = stats.get("replies", 0) + 1
                entries.append((message.timestamp, f"[{stamp(message.timestamp)}] {sender}: {text}"))
        for call in calls_by_chat.get(jik, []):
            other = _display(call["other"], directory, data) or "?"
            caller = _display(call["creator"], directory, data) or other
            sender = ME if call["from_me"] else (caller if call["group"] else other)
            entries.append((call["ts"], f"[{stamp(call['ts'])}] {sender}: {_call_text(call, other)}"))
            stats["call"] = stats.get("call", 0) + 1
        entries.sort(key=lambda e: e[0])  # stable: messages keep their database order
        with open(os.path.join(output, f"{stem}.txt"), "x", encoding="utf8") as f:
            f.write("".join(line + "\n" for _, line in entries))
        stats["files"] += 1
        stats["written"] += len(entries)
    logging.info(f"Revocación TXT: {stats}")
    return stats
