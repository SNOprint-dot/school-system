from flask_login import UserMixin
from db_config import get_db_connection

class User(UserMixin):
    def __init__(self, user_id, email, role):
        self.id = str(user_id) 
        self.email = email
        self.role = role

    @staticmethod
    def get(user_id):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id, email, role FROM system_users WHERE user_id = %s", (user_id,))
        user_data = cur.fetchone()
        cur.close()
        conn.close()
        if user_data:
            return User(user_data['user_id'], user_data['email'], user_data['role'])
        return None
