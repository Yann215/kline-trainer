from datetime import datetime, timedelta, timezone

def cn_today():
    return datetime.now(timezone(timedelta(hours=8))).date()
