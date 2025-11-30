active_users = set()

def register_user(user_id: int, username: str):
    """Регистрирует каждого пользователя, который общался с ботом."""
    active_users.add((user_id, username))
