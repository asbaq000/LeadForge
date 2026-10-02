"""
Turn a keyword into candidate handles, and decide whether an account is
actually about that keyword.

Why guessing handles at all: Instagram's search and hashtag endpoints both
answer `login_required` to anonymous callers, so there is no query-by-keyword
route logged out. What *does* work is that niche accounts overwhelmingly name
themselves after their niche — `wildlifedocumentary`, `history.documentary`,
`woodworking_projects`. Guessing those costs one cheap request each and gives
the related-profiles crawl somewhere to start.
"""

import re

# Words too generic to carry a niche on their own. A candidate still has to
# match every *other* token, so "Cozy Lifestyle Vlog" needs cozy + lifestyle.
GENERIC_TOKENS = {"the", "and", "a", "of", "in", "my", "4k", "series", "channel"}

SEPARATORS = ["", ".", "_"]
SUFFIXES = ["", "s", "official", "hq", "tv", "daily", "world"]


def tokenize(keyword):
    """'WW2 History Documentary' -> ['ww2', 'history', 'documentary']"""
    return [t for t in re.split(r"[^a-z0-9]+", keyword.lower()) if t]


def significant_tokens(keyword):
    tokens = [t for t in tokenize(keyword) if t not in GENERIC_TOKENS]
    return tokens or tokenize(keyword)


def candidate_handles(keyword, limit=6):
    """
    Build plausible usernames for a keyword, most-likely first.

    Instagram handles cap at 30 characters, so long keywords also get a
    shortened two-token form.
    """
    tokens = significant_tokens(keyword)
    if not tokens:
        return []

    forms = [tokens]
    if len(tokens) > 2:
        forms.append(tokens[:2])
        forms.append([tokens[0], tokens[-1]])

    # Suffix outermost so the budget goes on spelling variants of the plain
    # name (wildlifedocumentary / wildlife.documentary / wildlife_documentary)
    # before it goes on wildlifedocumentaryhq — the plain forms are far more
    # likely to be the account that actually exists.
    seen, handles = set(), []
    for form in forms:
        for suffix in SUFFIXES:
            for sep in SEPARATORS:
                handle = sep.join(form) + suffix
                if len(handle) > 30 or handle in seen:
                    continue
                seen.add(handle)
                handles.append(handle)
                if len(handles) >= limit:
                    return handles
    return handles


def _haystack(record):
    return " ".join(
        [
            record.get("username", ""),
            record.get("full_name", ""),
            record.get("biography", ""),
            record.get("category", ""),
        ]
    ).lower()


def matches_keyword(record, keyword):
    """
    True when the account looks like it's actually in this niche.

    Requires every significant token of the keyword to appear somewhere in
    the handle, name, bio or category — or the glued-together form to appear
    verbatim. Loose enough to catch "Wildlife filmmaker | documentary work",
    strict enough to reject a generic travel account for "Deep Sea Documentary".
    """
    haystack = _haystack(record)
    tokens = significant_tokens(keyword)
    if not tokens:
        return False
    if "".join(tokens) in re.sub(r"[^a-z0-9]", "", haystack):
        return True
    return all(token in haystack for token in tokens)


def matching_keywords(record, keywords):
    """Every keyword from the list that this account plausibly belongs to."""
    return [kw for kw in keywords if matches_keyword(record, kw)]
