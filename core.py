import os
import decimal
import boto3
from datetime import datetime, date
from functools import wraps
from flask import jsonify
from flask_login import UserMixin, current_user
from werkzeug.security import generate_password_hash
from db_config import get_db_connection

# --- CLOUD STORAGE SETTINGS ---
AWS_BUCKET_NAME = os.environ.get('AWS_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
    region_name=os.environ.get('AWS_REGION', 'eu-north-1')
)

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

def log_audit(school_id, user_email, action, target):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (school_id, user_email, action, target))
        conn.commit()
    except Exception: pass
    finally: cur.close(); conn.close()

class User(UserMixin):
    def __init__(self, user_id, email, role, linked_student_id=None, school_id=None):
        self.id = str(user_id)
        self.email = email
        self.role = role
        self.linked_student_id = linked_student_id
        self.school_id = school_id

    @staticmethod
    def get(user_id):
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, email, role, linked_student_id, school_id FROM system_users WHERE user_id = %s", (user_id,))
        user_data = cur.fetchone()
        cur.close(); conn.close()
        if user_data: return User(user_data['user_id'], user_data['email'], user_data['role'], user_data['linked_student_id'], user_data['school_id'])
        return None

def require_active_subscription(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.role == 'superadmin': return f(*args, **kwargs)
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT subscription_expiry_date FROM institutions WHERE school_id = %s", (current_user.school_id,))
        school = cur.fetchone(); cur.close(); conn.close()
        if not school or school['subscription_expiry_date'] < datetime.now().date(): 
            return jsonify({"error": "ACCESS LOCKED: Your annual subscription has expired."}), 402 
        return f(*args, **kwargs)
    return decorated_function

def initialize_database():
    conn = get_db_connection(); cur = conn.cursor()
    
    # Core Platform
    cur.execute("CREATE TABLE IF NOT EXISTS institutions (school_id SERIAL PRIMARY KEY, school_name VARCHAR(150) NOT NULL UNIQUE, subscription_expiry_date DATE NOT NULL)")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS address VARCHAR(255) DEFAULT 'Ghana'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS phone VARCHAR(50) DEFAULT '0000000000'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS primary_color VARCHAR(20) DEFAULT '#0f4c81'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS logo_key VARCHAR(255)")
    
    # Students & Users
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
    
    # Security & Academics
    cur.execute("CREATE TABLE IF NOT EXISTS audit_logs (log_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, user_email VARCHAR(100), action VARCHAR(255), target VARCHAR(255), timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS subjects (subject_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100) NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS grades (grade_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, subject_id INTEGER REFERENCES subjects(subject_id) ON DELETE CASCADE, class_score INTEGER NOT NULL, exam_score INTEGER NOT NULL, total_score INTEGER NOT NULL, waec_grade VARCHAR(2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL, teacher_remarks VARCHAR(255))")
    
    # Finance
    cur.execute("CREATE TABLE IF NOT EXISTS fees (fee_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, description VARCHAR(255) NOT NULL, amount_due DECIMAL(10, 2) NOT NULL, date_issued TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("ALTER TABLE fees ADD COLUMN IF NOT EXISTS fee_category VARCHAR(50) DEFAULT 'General'")
    cur.execute("ALTER TABLE fees ADD COLUMN IF NOT EXISTS academic_year VARCHAR(9) DEFAULT 'Unknown'")
    cur.execute("ALTER TABLE fees ADD COLUMN IF NOT EXISTS term VARCHAR(20) DEFAULT 'Unknown'")
    cur.execute("CREATE TABLE IF NOT EXISTS payments (payment_id SERIAL PRIMARY KEY, fee_id INTEGER REFERENCES fees(fee_id) ON DELETE CASCADE, amount_paid DECIMAL(10, 2) NOT NULL, payment_method VARCHAR(50) NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS expenses (expense_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, category VARCHAR(50) NOT NULL, description VARCHAR(255) NOT NULL, amount DECIMAL(10, 2) NOT NULL, date_incurred TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    
    # Operational Modules
    cur.execute("CREATE TABLE IF NOT EXISTS attendance (attendance_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, record_date DATE NOT NULL, status VARCHAR(20) NOT NULL, UNIQUE(student_id, record_date))")
    cur.execute("CREATE TABLE IF NOT EXISTS transport_routes (route_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, route_name VARCHAR(100) NOT NULL, driver_name VARCHAR(100), fare DECIMAL(10, 2) DEFAULT 0.00)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_items (item_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, item_name VARCHAR(100) NOT NULL, price DECIMAL(10, 2) NOT NULL, stock INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_sales (sale_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, item_name VARCHAR(100), quantity INTEGER, total_cost DECIMAL(10, 2), sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS exeats (exeat_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, exeat_type VARCHAR(50), reason VARCHAR(255), expected_return DATE, status VARCHAR(20) DEFAULT 'Active', issue_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS sick_bay_logs (log_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, symptoms VARCHAR(255), treatment VARCHAR(255), log_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS academic_calendar (event_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, event_title VARCHAR(150), event_date DATE, description VARCHAR(255))")

    # CBT & Lesson Plans
    cur.execute("CREATE TABLE IF NOT EXISTS lesson_plans (plan_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, teacher_email VARCHAR(100), subject VARCHAR(100), class_name VARCHAR(100), week_number INTEGER, topic VARCHAR(255), plan_content TEXT, status VARCHAR(20) DEFAULT 'Pending', admin_remarks VARCHAR(255), submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS cbt_quizzes (quiz_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100), class_name VARCHAR(100), title VARCHAR(150), academic_year VARCHAR(9), term VARCHAR(20), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS cbt_questions (question_id SERIAL PRIMARY KEY, quiz_id INTEGER REFERENCES cbt_quizzes(quiz_id) ON DELETE CASCADE, question_text TEXT NOT NULL, opt_a VARCHAR(255), opt_b VARCHAR(255), opt_c VARCHAR(255), opt_d VARCHAR(255), correct_opt VARCHAR(1))")

    # Superadmin Genesis Account
    cur.execute("SELECT * FROM system_users WHERE role = 'superadmin'")
    if not cur.fetchone():
        hashed_sa = generate_password_hash('ceo123')
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s)", ('superadmin@engine.com', hashed_sa, 'superadmin'))
    
    conn.commit(); cur.close(); conn.close()
