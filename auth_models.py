from flask_login import UserMixin
from db_config import get_db_connection

class User(UserMixin):
    # We added linked_student_id=None here so it can accept the 5th argument!
    def __init__(self, user_id, email, role, linked_student_id=None):
        self.id = str(user_id)
        self.email = email
        self.role = role
        self.linked_student_id = linked_student_id

    @staticmethod
    def get(user_id):
        conn = get_db_connection()
        cur = conn.cursor()
        # We upgraded the SQL query to fetch the linked_student_id as well
        cur.execute("SELECT user_id, email, role, linked_student_id FROM system_users WHERE user_id = %s", (user_id,))
        user_data = cur.fetchone()
        cur.close()
        conn.close()
        
        if user_data:
            return User(
                user_data['user_id'], 
                user_data['email'], 
                user_data['role'], 
                user_data['linked_student_id']
            )
        return None
