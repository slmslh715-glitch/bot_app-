TIER = "business"

FEATURES = {
    "starter": {
        "save_chat": False,
        "admin_panel": False,
        "stats_page": False,
        "arabic_admin": False,
        "custom_feature": False
    },
    "business": {
        "save_chat": True,
        "admin_panel": True,
        "stats_page": False,
        "arabic_admin": False,
        "custom_feature": False
    },
    "premium": {
        "save_chat": True,
        "admin_panel": True,
        "stats_page": True,
        "arabic_admin": True,
        "custom_feature": False
    },
    "promax": {
        "save_chat": True,
        "admin_panel": True,
        "stats_page": True,
        "arabic_admin": True,
        "custom_feature": True
    }
}

CURRENT_TIER_FEATURES = FEATURES[TIER]