"""Sample module for exercising the prcheck reviewer (intentionally flawed)."""


def get_user(db, user_id):
    # Look up a user by id.
    query = f"SELECT * FROM users WHERE id = '{user_id}'"
    return db.execute(query).fetchone()


def first_admin(users):
    admins = [u for u in users if u.get("is_admin")]
    return admins[0]
