"""Discord content bounds, including astral characters counted as UTF-16 units."""


def chunks(text: str, limit: int = 2000) -> list[str]:
    pages, page, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xffff else 1
        if size + width > limit:
            pages.append(''.join(page))
            page, size = [], 0
        page.append(char)
        size += width
    if page:
        pages.append(''.join(page))
    return pages
