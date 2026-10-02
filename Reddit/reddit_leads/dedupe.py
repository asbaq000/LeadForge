"""Duplicate removal.

The same gig reaches us several times: one post is returned by more than one
query, and some posters re-post the identical ad every few days under a new
ID. Four keys catch all of that.
"""

import re

STOPWORDS = {
    "a", "an", "the", "for", "to", "of", "and", "or", "in", "on", "at", "with",
    "we", "our", "you", "your", "is", "are", "be", "need", "needed", "looking",
    "hiring", "hire", "job", "role", "remote", "usd", "hour", "hr", "week",
    "month", "paid", "pay", "help", "please", "new", "up",
}


# Two unrelated clients can both post "Looking for an AI developer", so a
# title alone only identifies a post when it carries enough distinct words.
MIN_CROSS_AUTHOR_TOKENS = 4


def title_signature(title):
    """Order-insensitive fingerprint of a title's meaningful words."""
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    meaningful = sorted({w for w in words if w not in STOPWORDS and len(w) > 2})
    return " ".join(meaningful)


def keys_for(post):
    """All identities under which a post might already have been seen."""
    keys = []
    post_id = post.get("id")
    if post_id:
        keys.append(("id", post_id))

    author = (post.get("author") or "").lower()
    signature = title_signature(post.get("title"))
    if signature:
        # Same wording from the same author = a re-post of one ad.
        if author and author != "[deleted]":
            keys.append(("author+title", author + "|" + signature))
        # Same wording from anyone = a cross-post or copy-paste ad, but only
        # trust that when the title is specific enough to be unmistakable.
        if len(signature.split()) >= MIN_CROSS_AUTHOR_TOKENS:
            keys.append(("title", signature))

    url = post.get("url_overridden_by_dest") or ""
    if url and "reddit.com" not in url:
        keys.append(("link", url.split("?")[0].rstrip("/").lower()))
    return keys


def deduplicate(leads, key_fn=keys_for, prefer=None):
    """Keep one lead per identity.

    `prefer` picks the survivor when two leads collide; the default keeps the
    one already stored (leads are passed in best-first).
    """
    seen = {}
    kept = []
    duplicates = 0
    for lead in leads:
        post = lead["post"] if "post" in lead else lead
        identities = key_fn(post)
        clash = next((seen[k] for k in identities if k in seen), None)
        if clash is not None:
            duplicates += 1
            if prefer and prefer(lead, kept[clash]) is lead:
                kept[clash] = lead
            for key in identities:
                seen.setdefault(key, clash)
            continue
        index = len(kept)
        kept.append(lead)
        for key in identities:
            seen[key] = index
    return kept, duplicates
