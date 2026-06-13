def merge_transfer_type(category: str, transfer_type: str) -> tuple[str, bool]:
    """
    Apply transfer_type to Internal Transfer rows.
    Returns (final_category, needs_review).
    needs_review=True when transfer_type=unknown.
    """
    if category != "Internal Transfer":
        return category, False
    t = (transfer_type or "").strip().lower()
    if t == "self":
        return "Internal Transfer — Self", False
    elif t in ("other", "others"):
        return "Internal Transfer", False
    elif t == "unknown":
        return "Internal Transfer", True
    else:
        return "Internal Transfer", False
