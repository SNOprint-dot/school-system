import os
import json
import decimal
import boto3
from datetime import datetime, date
from flask_login import UserMixin
from db_config import get_db_connection
from werkzeug.security import generate_password_hash

AWS_BUCKET_NAME = os.environ.get('AWS_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
    region_name=os.environ.get('AWS_REGION', 'eu-north-1')
) if os.environ.get('AWS_ACCESS_KEY_ID') else None

def custom_json_serializer(obj):
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    if isinstance(obj, decimal.Decimal): return float(obj)
    raise TypeError(f"Type {type(obj)} not serializable")

def get_waec_grade(score):
    if score >= 80: return 'A1'
    elif score >= 70: return 'B2'
    elif score >= 65: return 'B3'
    elif score >= 60: return 'C4'
    elif score >= 55: return 'C5'
    elif score >= 50: return 'C6'
    elif score >= 45: return 'D7'
    elif score >= 40: return 'E8'
    else: return 'F9'

class User(UserMixin):
    def __init__(self, user_id, email, role, linked_student_id=None, school_id=None):
        self.id = str(user_id)
        self.email = email
        self.role = role
        self.linked_student_id = linked_student_id
        self.school_id = school_id

    @staticmethod
    def get(user_id):
        try:
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("SELECT user_id, email, role, linked_student_id, school_id FROM system_users WHERE user_id = %s", (user_id,))
            user_data = cur.fetchone(); cur.close(); conn.close()
            if user_data: return User(user_data['user_id'], user_data['email'], user_data['role'], user_data['linked_student_id'], user_data['school_id'])
            return None
        except Exception:
            return None

def initialize_database():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS institutions (school_id SERIAL PRIMARY KEY, school_name VARCHAR(150) NOT NULL UNIQUE, subscription_expiry_date DATE NOT NULL)")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS address VARCHAR(255) DEFAULT 'Ghana'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS phone VARCHAR(50) DEFAULT '0000000000'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS primary_color VARCHAR(20) DEFAULT '#0f4c81'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS logo_key VARCHAR(255)")
    
    cur.execute("CREATE TABLE IF NOT EXISTS students (student_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, first_name VARCHAR(100) NOT NULL, last_name VARCHAR(100) NOT NULL, guardian_name VARCHAR(100) NOT NULL, guardian_contact VARCHAR(20) NOT NULL, enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS boarding_status VARCHAR(20) DEFAULT 'Day'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS house VARCHAR(100) DEFAULT 'Unassigned'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS current_class VARCHAR(100) DEFAULT 'Unassigned'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS transport_route VARCHAR(100) DEFAULT 'None'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS photo_key VARCHAR(255)")
    
    cur.execute("CREATE TABLE IF NOT EXISTS system_users (user_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, email VARCHAR(100) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, linked_student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE)")
    cur.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS phone VARCHAR(20)")
    cur.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS subject VARCHAR(100)")
    cur.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS base_salary DECIMAL(10,2) DEFAULT 0.00")
    
    cur.execute("CREATE TABLE IF NOT EXISTS audit_logs (log_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, user_email VARCHAR(100), action VARCHAR(255), target VARCHAR(255), timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS subjects (subject_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100) NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS grades (grade_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, subject_id INTEGER REFERENCES subjects(subject_id) ON DELETE CASCADE, class_score INTEGER NOT NULL, exam_score INTEGER NOT NULL, total_score INTEGER NOT NULL, waec_grade VARCHAR(2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL, teacher_remarks VARCHAR(255))")
    
    cur.execute("CREATE TABLE IF NOT EXISTS fees (fee_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, description VARCHAR(255) NOT NULL, amount_due DECIMAL(10, 2) NOT NULL, date_issued TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("ALTER TABLE fees ADD COLUMN IF NOT EXISTS fee_category VARCHAR(50) DEFAULT 'General'")
    cur.execute("ALTER TABLE fees ADD COLUMN IF NOT EXISTS academic_year VARCHAR(9) DEFAULT 'Unknown'")
    cur.execute("ALTER TABLE fees ADD COLUMN IF NOT EXISTS term VARCHAR(20) DEFAULT 'Unknown'")

    cur.execute("CREATE TABLE IF NOT EXISTS payments (payment_id SERIAL PRIMARY KEY, fee_id INTEGER REFERENCES fees(fee_id) ON DELETE CASCADE, amount_paid DECIMAL(10, 2) NOT NULL, payment_method VARCHAR(50) NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS expenses (expense_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, category VARCHAR(50) NOT NULL, description VARCHAR(255) NOT NULL, amount DECIMAL(10, 2) NOT NULL, date_incurred TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS attendance (attendance_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, record_date DATE NOT NULL, status VARCHAR(20) NOT NULL, UNIQUE(student_id, record_date))")
    
    # Advanced Modules
    cur.execute("CREATE TABLE IF NOT EXISTS transport_routes (route_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, route_name VARCHAR(100) NOT NULL, driver_name VARCHAR(100), fare DECIMAL(10, 2) DEFAULT 0.00)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_items (item_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, item_name VARCHAR(100) NOT NULL, price DECIMAL(10, 2) NOT NULL, stock INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_sales (sale_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, item_name VARCHAR(100), quantity INTEGER, total_cost DECIMAL(10, 2), sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS exeats (exeat_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, exeat_type VARCHAR(50), reason VARCHAR(255), expected_return DATE, status VARCHAR(20) DEFAULT 'Active', issue_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS sick_bay_logs (log_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, symptoms VARCHAR(255), treatment VARCHAR(255), log_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS academic_calendar (event_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, event_title VARCHAR(150), event_date DATE, description VARCHAR(255))")
    cur.execute("CREATE TABLE IF NOT EXISTS lesson_plans (plan_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, teacher_email VARCHAR(100), subject VARCHAR(100), class_name VARCHAR(100), week_number INTEGER, topic VARCHAR(255), plan_content TEXT, status VARCHAR(20) DEFAULT 'Pending', admin_remarks VARCHAR(255), submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS cbt_quizzes (quiz_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100), class_name VARCHAR(100), title VARCHAR(150), academic_year VARCHAR(9), term VARCHAR(20), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS cbt_questions (question_id SERIAL PRIMARY KEY, quiz_id INTEGER REFERENCES cbt_quizzes(quiz_id) ON DELETE CASCADE, question_text TEXT NOT NULL, opt_a VARCHAR(255), opt_b VARCHAR(255), opt_c VARCHAR(255), opt_d VARCHAR(255), correct_opt VARCHAR(1))")

    cur.execute("SELECT * FROM system_users WHERE role = 'superadmin'")
    if not cur.fetchone():
        hashed_sa = generate_password_hash('ceo123')
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s)", ('superadmin@engine.com', hashed_sa, 'superadmin'))
    
    conn.commit(); cur.close(); conn.close()

def automated_weekly_backup():
    if not AWS_BUCKET_NAME: return
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT school_id, school_name FROM institutions")
    schools = cur.fetchall()
    for school in schools:
        school_id = school['school_id']; school_name = school['school_name']
        backup = {"school_name": school_name, "export_date": datetime.now().isoformat(), "data": {}}
        cur.execute("SELECT * FROM subjects WHERE school_id = %s", (school_id,)); backup['data']['subjects'] = cur.fetchall()
        cur.execute("SELECT * FROM students WHERE school_id = %s", (school_id,)); backup['data']['students'] = cur.fetchall()
        cur.execute("SELECT * FROM fees WHERE school_id = %s", (school_id,)); backup['data']['fees'] = cur.fetchall()
        cur.execute("SELECT p.* FROM payments p JOIN fees f ON p.fee_id = f.fee_id WHERE f.school_id = %s", (school_id,)); backup['data']['payments'] = cur.fetchall()
        cur.execute("SELECT * FROM grades WHERE school_id = %s", (school_id,)); backup['data']['grades'] = cur.fetchall()
        cur.execute("SELECT * FROM expenses WHERE school_id = %s", (school_id,)); backup['data']['expenses'] = cur.fetchall()
        json_data = json.dumps(backup, default=custom_json_serializer)
        filename = f"Automated_Backups/{school_name.replace(' ', '_')}/Backup_{datetime.now().strftime('%Y%m%d')}.json"
        try: s3_client.put_object(Bucket=AWS_BUCKET_NAME, Key=filename, Body=json_data)
        except Exception: pass
    cur.close(); conn.close()
