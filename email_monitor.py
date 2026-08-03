"""Low-level Outlook COM/MAPI helpers for the Email Auto-Import monitor
(Settings > Email Auto-Import).

This module deliberately knows nothing about Flask, the database, or how a
bank statement gets parsed — it only knows how to talk to Outlook. The
orchestration (which folders to check, what to do with what it finds) lives
in app.py, exactly the same split as importer.py (parsing) vs app.py
(deciding what to do with the parsed rows).

Modeled directly on the polling approach already proven out in Kevin's
outlook_tools_v2.0/monitor.py: win32com.client.Dispatch("Outlook.Application"),
GetNamespace("MAPI"), Items.Restrict("[UnRead] = True"), and
att.SaveAsFile(...) to read an attachment's bytes. Requires Outlook to
actually be open on this machine — there is no way around that with COM
automation, and every function here raises OutlookUnavailable (never lets a
raw COM error escape) when it isn't, so callers can just treat that as "skip
this check, try again next time".

Windows + pywin32 only. On any other platform (or if pywin32 isn't
installed), OUTLOOK_AVAILABLE is False and every function raises
OutlookUnavailable immediately instead of failing to import.
"""
import os
import tempfile

try:
    import pythoncom
    import win32com.client
    OUTLOOK_AVAILABLE = True
except ImportError:  # not on Windows, or pywin32 isn't installed
    OUTLOOK_AVAILABLE = False


# Attachment extensions the importer knows how to parse (see importer.sniff_file).
SUPPORTED_EXTS = (".csv", ".xlsx", ".xlsm", ".pdf")


class OutlookUnavailable(Exception):
    """Outlook isn't reachable right now — not installed, not open, or this
    isn't Windows. Always safe to treat as "try again on the next poll"."""


def outlook_namespace():
    """Connects to the running Outlook application and returns its MAPI
    namespace. Raises OutlookUnavailable (never a raw COM exception) if
    Outlook isn't open or pywin32 isn't available."""
    if not OUTLOOK_AVAILABLE:
        raise OutlookUnavailable(
            "Email Auto-Import needs pywin32 and Outlook, and only runs on Windows."
        )
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass  # already initialized on this thread — fine
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        return ol.GetNamespace("MAPI")
    except Exception as exc:
        raise OutlookUnavailable(f"Cannot connect to Outlook — make sure it is open. ({exc})")


def list_folders():
    """Returns {account_display_name: [full_folder_path, ...]} for every
    mail folder in every account/store Outlook currently has open — used by
    the Settings UI to offer a folder picker instead of making someone type
    an Outlook folder path by hand. Each full_folder_path looks like
    "<Account>\\<Folder>\\<SubFolder>" and is exactly what resolve_folder()
    below expects back."""
    ns = outlook_namespace()
    result = {}
    for store in ns.Stores:
        try:
            root = store.GetRootFolder()
            name = store.DisplayName
            paths = []

            def walk(folder, prefix):
                path = f"{prefix}\\{folder.Name}"
                paths.append(path)
                try:
                    for sub in folder.Folders:
                        walk(sub, path)
                except Exception:
                    pass

            for sub in root.Folders:
                walk(sub, name)
            result[name] = paths
        except Exception:
            continue
    return result


def resolve_folder(ns, full_path):
    """Resolves a "<Account>\\<Folder>\\..." path (as produced by
    list_folders(), and as stored in EmailImportRule.folder_path) to a live
    Outlook MAPIFolder COM object. Raises ValueError with a message safe to
    show to a person if the account or a sub-folder can't be found (e.g. the
    folder was renamed or deleted since the rule was set up)."""
    parts = [p for p in full_path.split("\\") if p]
    if len(parts) < 2:
        raise ValueError(f"Not a valid Outlook folder path: {full_path!r}")
    account, sub_parts = parts[0], parts[1:]
    for store in ns.Stores:
        if store.DisplayName == account:
            node = store.GetRootFolder()
            for part in sub_parts:
                found = False
                for sub in node.Folders:
                    if sub.Name == part:
                        node = sub
                        found = True
                        break
                if not found:
                    raise ValueError(f"Sub-folder '{part}' not found under Outlook account '{account}'.")
            return node
    raise ValueError(f"Outlook account '{account}' not found — is it still set up in Outlook?")


def unread_items_with_attachment(folder):
    """Yields (item, filename, file_bytes) for every unread item in `folder`
    that has an attachment with a supported extension (.csv/.xlsx/.xlsm/
    .pdf) — the first such attachment only, since a bank statement email is
    expected to carry a single statement file.

    Snapshots the Restrict() result into a plain list before yielding, so
    the caller marking earlier items read mid-loop can't disturb Outlook's
    live "[UnRead] = True" view of later ones.
    """
    try:
        restricted = folder.Items.Restrict("[UnRead] = True")
    except Exception as exc:
        raise OutlookUnavailable(f"Could not read folder '{getattr(folder, 'Name', '?')}': {exc}")

    items = list(restricted)
    for item in items:
        try:
            if not getattr(item, "UnRead", False):
                continue  # changed since the snapshot (e.g. read on another device)
            filename, data = _first_supported_attachment(item) or (None, None)
        except Exception:
            continue
        if filename is None:
            continue
        yield item, filename, data


def _first_supported_attachment(item):
    try:
        count = item.Attachments.Count
    except Exception:
        return None
    for i in range(1, count + 1):
        att = item.Attachments.Item(i)
        filename = getattr(att, "FileName", "") or ""
        ext = os.path.splitext(filename)[1].lower()
        if ext not in SUPPORTED_EXTS:
            continue
        tmp_dir = tempfile.mkdtemp(prefix="rm_email_import_")
        dest = os.path.join(tmp_dir, filename)
        try:
            att.SaveAsFile(dest)
            with open(dest, "rb") as f:
                data = f.read()
            return filename, data
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
    return None


def mark_read(item):
    """Marks an Outlook item as read — done only after it's been fully
    processed (auto-committed, or turned into a pending-review import
    session), so a crash mid-processing leaves it unread and picked up
    again on the next poll rather than silently skipped."""
    item.UnRead = False
    item.Save()


def message_metadata(item):
    """Best-effort subject/sender for an item, used only for display in the
    Dashboard/Settings — never depended on for matching logic."""
    subject = ""
    sender = ""
    try:
        subject = getattr(item, "Subject", "") or ""
    except Exception:
        pass
    try:
        sender = getattr(item, "SenderName", "") or getattr(item, "SenderEmailAddress", "") or ""
    except Exception:
        pass
    return subject, sender
