"""Operations the assistant may prepare for user confirmation."""

REWARDS = (
    "signin", "mail", "base", "dispatch", "achievement", "guild", "guild_weekly", "recruit",
    "friend_support", "send_meal", "reactor", "potion", "starforce", "tesseract",
)
ACCOUNT = ("login", "redeem", "donate", "cancel", "unbind", "logout")
QUERY = ("status", "receipt", "encyclopedia", "stages", "equipment", "account_view")
LONG = (
    "long_info", "activity", "abyss", "buy_batch", "refresh_secret", "refresh_star_source",
    "schedule_set", "schedule_status", "schedule_stop", "sweep_illusion", "member_purchase",
    "sweep_hunt", "sweep_elf", "sweep_activity",
)
ALLOWED_OPERATIONS = frozenset(REWARDS + ACCOUNT + QUERY + LONG)
