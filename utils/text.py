"""Small text helpers shared across handlers."""

# Telegram rejects messages longer than 4096 chars. Leave a little headroom.
TG_LIMIT = 4096
CHUNK = 3900


def chunk_text(text):
    """Split a long message into Telegram-safe pieces, trying to break on
    newlines so we don't cut sentences in half."""
    if len(text) <= TG_LIMIT:
        return [text]

    pieces = []
    remaining = text
    while len(remaining) > CHUNK:
        # look for a newline to break on within the window
        split = remaining.rfind("\n", 0, CHUNK)
        if split == -1:
            split = CHUNK
        pieces.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    if remaining:
        pieces.append(remaining)
    return pieces


async def reply_long(message, text, **kwargs):
    """reply_text that transparently handles over-long messages."""
    parts = chunk_text(text)
    sent = None
    for part in parts:
        sent = await message.reply_text(part, **kwargs)
    return sent


def display_name(user):
    """Readable name for a Telegram user."""
    name = user.full_name or user.first_name or "Someone"
    return name.strip() or "Someone"
