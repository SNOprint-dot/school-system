import os
import psycopg2
import csv
import json
import decimal
import boto3
import base64
import uuid
import requests
from io import StringIO
from functools import wraps
from datetime import datetime, date
from flask import Flask, jsonify, request, render_template_string, Response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from db_config import get_db_connection
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secure-enterprise-key')

login_manager = LoginManager()
login_manager.init_app(app)

# --- CLOUD STORAGE SETTINGS ---
AWS_BUCKET_NAME = os.environ.get('AWS_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
    region_name=os.environ.get('AWS_REGION', 'eu-north-1')
)

# --- PAYMENT GATEWAY CONFIG ---
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')

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
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, email, role, linked_student_id, school_id FROM system_users WHERE user_id = %s", (user_id,))
        user_data = cur.fetchone()
        cur.close(); conn.close()
        if user_data: return User(user_data['user_id'], user_data['email'], user_data['role'], user_data['linked_student_id'], user_data['school_id'])
        return None

@login_manager.user_loader
def load_user(user_id): return User.get(user_id)

def require_active_subscription(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.role == 'superadmin': return f(*args, **kwargs)
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT subscription_expiry_date FROM institutions WHERE school_id = %s", (current_user.school_id,))
        school = cur.fetchone(); cur.close(); conn.close()
        if not school or school['subscription_expiry_date'] < datetime.now().date(): return jsonify({"error": "ACCESS LOCKED: Your annual subscription has expired."}), 402 
        return f(*args, **kwargs)
    return decorated_function

# --- CORE DATABASE ENGINE ---
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
    
    # --- ENTERPRISE TABLES ---
    cur.execute("CREATE TABLE IF NOT EXISTS transport_routes (route_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, route_name VARCHAR(100) NOT NULL, driver_name VARCHAR(100), fare DECIMAL(10, 2) DEFAULT 0.00)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_items (item_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, item_name VARCHAR(100) NOT NULL, price DECIMAL(10, 2) NOT NULL, stock INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_sales (sale_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, item_name VARCHAR(100), quantity INTEGER, total_cost DECIMAL(10, 2), sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS exeats (exeat_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, exeat_type VARCHAR(50), reason VARCHAR(255), expected_return DATE, status VARCHAR(20) DEFAULT 'Active', issue_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS sick_bay_logs (log_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, symptoms VARCHAR(255), treatment VARCHAR(255), log_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS academic_calendar (event_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, event_title VARCHAR(150), event_date DATE, description VARCHAR(255))")

    # --- GES LESSON PLANS & CBT TABLES ---
    cur.execute("CREATE TABLE IF NOT EXISTS lesson_plans (plan_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, teacher_email VARCHAR(100), subject VARCHAR(100), class_name VARCHAR(100), week_number INTEGER, topic VARCHAR(255), plan_content TEXT, status VARCHAR(20) DEFAULT 'Pending', admin_remarks VARCHAR(255), submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS cbt_quizzes (quiz_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100), class_name VARCHAR(100), title VARCHAR(150), academic_year VARCHAR(9), term VARCHAR(20), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS cbt_questions (question_id SERIAL PRIMARY KEY, quiz_id INTEGER REFERENCES cbt_quizzes(quiz_id) ON DELETE CASCADE, question_text TEXT NOT NULL, opt_a VARCHAR(255), opt_b VARCHAR(255), opt_c VARCHAR(255), opt_d VARCHAR(255), correct_opt VARCHAR(1))")

    cur.execute("SELECT * FROM system_users WHERE role = 'superadmin'")
    if not cur.fetchone():
        hashed_sa = generate_password_hash('ceo123')
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s)", ('superadmin@engine.com', hashed_sa, 'superadmin'))
    conn.commit(); cur.close(); conn.close()

@app.route('/api/setup_db')
def setup_db():
    initialize_database()
    return jsonify({"message": "Multi-Tenant SaaS Engine Ready! Database patched."})

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

scheduler = BackgroundScheduler()
scheduler.add_job(func=automated_weekly_backup, trigger="cron", day_of_week='sun', hour=23, minute=59)
scheduler.start()

# --- AUTH & SECURITY ENGINE ---
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT user_id, email, password_hash, role, linked_student_id, school_id FROM system_users WHERE email = %s", (data.get('email'),))
    user_data = cur.fetchone(); cur.close(); conn.close()
    if user_data and check_password_hash(user_data['password_hash'], data.get('password')):
        user = User(user_data['user_id'], user_data['email'], user_data['role'], user_data['linked_student_id'], user_data['school_id'])
        login_user(user); return jsonify({"message": f"Logged in as {user.role}."})
    return jsonify({"error": "Invalid credentials!"}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout(): logout_user(); return jsonify({"message": "Logged out safely."})

@app.route('/api/verify_password', methods=['POST'])
@login_required
def verify_password():
    d = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT password_hash FROM system_users WHERE user_id = %s", (current_user.id,))
    user = cur.fetchone(); cur.close(); conn.close()
    if user and check_password_hash(user['password_hash'], d.get('password')): return jsonify({"message": "Vault unlocked."}), 200
    return jsonify({"error": "Incorrect password. Access denied."}), 403

@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    d = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT password_hash FROM system_users WHERE user_id = %s", (current_user.id,))
        user = cur.fetchone()
        if not check_password_hash(user['password_hash'], d.get('current_password')): return jsonify({"error": "Incorrect current password."}), 403
        new_hash = generate_password_hash(d.get('new_password'))
        cur.execute("UPDATE system_users SET password_hash = %s WHERE user_id = %s", (new_hash, current_user.id))
        conn.commit(); return jsonify({"message": "Your private password has been successfully updated!"}), 200
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/admin/reset_password', methods=['POST'])
@login_required
def admin_reset_password():
    if current_user.role != 'admin': return jsonify({"error": "Admin clearance required."}), 403
    d = request.get_json(); new_hash = generate_password_hash(d.get('new_password')); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE system_users SET password_hash = %s WHERE email = %s AND school_id = %s RETURNING user_id", (new_hash, d.get('target_email'), current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": "User email not found in your school's database."}), 404
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Forced Password Reset', f"Target Email: {d.get('target_email')}"))
        conn.commit(); return jsonify({"message": f"Security override successful. Password reset for {d.get('target_email')}!"}), 200
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/superadmin/reset_admin', methods=['POST'])
@login_required
def superadmin_reset_admin():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    d = request.get_json(); new_hash = generate_password_hash(d.get('new_password')); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE system_users SET password_hash = %s WHERE email = %s RETURNING user_id", (new_hash, d.get('admin_email')))
        if not cur.fetchone(): return jsonify({"error": "Admin email not found in global registry."}), 404
        conn.commit(); return jsonify({"message": f"Global Override successful. Password reset for {d.get('admin_email')}!"}), 200
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/superadmin/credentials', methods=['POST'])
@login_required
def superadmin_update_credentials():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    d = request.get_json(); new_email = d.get('new_email'); new_pass = d.get('new_password')
    conn = get_db_connection(); cur = conn.cursor()
    try:
        if new_email: cur.execute("UPDATE system_users SET email = %s WHERE user_id = %s", (new_email, current_user.id))
        if new_pass: cur.execute("UPDATE system_users SET password_hash = %s WHERE user_id = %s", (generate_password_hash(new_pass), current_user.id))
        conn.commit(); return jsonify({"message": "Master Super Admin credentials updated successfully!"}), 200
    except psycopg2.IntegrityError: conn.rollback(); return jsonify({"error": "That email is already in use."}), 409
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

# --- IMMUTABLE AUDIT LOGS ENDPOINT ---
@app.route('/api/audit_logs', methods=['GET'])
@login_required
def get_audit_logs():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS') as time, user_email, action, target FROM audit_logs WHERE school_id = %s ORDER BY timestamp DESC LIMIT 200", (current_user.school_id,))
    logs = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": logs})

# --- SUPER ADMIN TENANT MANAGEMENT ---
@app.route('/api/superadmin/schools', methods=['GET'])
@login_required
def get_schools():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT school_id, school_name, primary_color, TO_CHAR(subscription_expiry_date, 'YYYY-MM-DD') as expiry_date, CASE WHEN subscription_expiry_date >= CURRENT_DATE THEN 'Active' ELSE 'Expired' END as status FROM institutions ORDER BY school_id")
    schools = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": schools})

@app.route('/api/superadmin/onboard', methods=['POST'])
@login_required
def onboard_school():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO institutions (school_name, subscription_expiry_date) VALUES (%s, CURRENT_DATE + INTERVAL '1 year') RETURNING school_id", (data.get('school_name'),))
        new_school_id = cur.fetchone()['school_id']
        cur.execute("INSERT INTO system_users (school_id, email, password_hash, role) VALUES (%s, %s, %s, 'admin')", (new_school_id, data.get('admin_email'), generate_password_hash(data.get('admin_password'))))
        conn.commit(); return jsonify({"message": f"Onboarded {data.get('school_name')}!"}), 201
    except psycopg2.IntegrityError: conn.rollback(); return jsonify({"error": "School name or Admin email already exists!"}), 409
    finally: cur.close(); conn.close()

@app.route('/api/superadmin/branding/<int:school_id>', methods=['POST'])
@login_required
def update_branding(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    d = request.get_json(); color = d.get('color') or '#0f4c81'; logo_b64 = d.get('logo_b64')
    conn = get_db_connection(); cur = conn.cursor()
    if logo_b64 and AWS_BUCKET_NAME:
        try:
            if ',' in logo_b64: logo_b64 = logo_b64.split(',')[1]
            logo_key = f"school_logos/{school_id}/{uuid.uuid4().hex}.png"
            s3_client.put_object(Bucket=AWS_BUCKET_NAME, Key=logo_key, Body=base64.b64decode(logo_b64), ContentType='image/png')
            cur.execute("UPDATE institutions SET primary_color = %s, logo_key = %s WHERE school_id = %s", (color, logo_key, school_id))
        except Exception: pass
    else: cur.execute("UPDATE institutions SET primary_color = %s WHERE school_id = %s", (color, school_id))
    conn.commit(); cur.close(); conn.close(); return jsonify({"message": "School branding updated successfully!"})

@app.route('/api/superadmin/renew/<int:school_id>', methods=['POST'])
@login_required
def renew_school(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE institutions SET subscription_expiry_date = CURRENT_DATE + INTERVAL '1 year' WHERE school_id = %s RETURNING school_name, subscription_expiry_date", (school_id,))
    updated = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return jsonify({"message": f"Contract Renewed! {updated['school_name']} active until {updated['subscription_expiry_date']}"})

# --- AUTOMATED MOMO / PAYSTACK PAYMENT ENGINE ---
@app.route('/api/momo/initialize', methods=['POST'])
@login_required
def momo_initialize():
    d = request.get_json()
    fee_id = int(d.get('fee_id'))
    amount = float(d.get('amount'))
    phone = str(d.get('phone') or '').strip()
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT student_id, amount_due FROM fees WHERE fee_id = %s", (fee_id,))
    fee = cur.fetchone()
    if not fee:
        cur.close(); conn.close()
        return jsonify({"error": "Fee bill not found."}), 404

    # If live/test Paystack API Key exists, trigger real mobile money checkout
    if PAYSTACK_SECRET_KEY:
        try:
            url = "https://api.paystack.co/transaction/initialize"
            headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
            payload = {
                "email": current_user.email,
                "amount": int(amount * 100), # Amount in pesewas
                "currency": "GHS",
                "channels": ["mobile_money", "card"],
                "metadata": {"fee_id": fee_id, "student_id": fee['student_id'], "phone": phone}
            }
            res = requests.post(url, json=payload, headers=headers)
            res_data = res.json()
            cur.close(); conn.close()
            if res_data.get('status'):
                return jsonify({"status": "paystack_redirect", "auth_url": res_data['data']['authorization_url']})
            else:
                return jsonify({"error": res_data.get('message', 'Paystack rejected transaction.')}), 400
        except Exception as e:
            cur.close(); conn.close()
            return jsonify({"error": f"Telecom gateway communication error: {str(e)}"}), 500
    else:
        # SANDBOX / SIMULATION MODE (Active while documentation is in process)
        try:
            cur.execute("INSERT INTO payments (fee_id, amount_paid, payment_method) VALUES (%s, %s, %s)", 
                        (fee_id, amount, f"MTN MoMo Sandbox ({phone})"))
            cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", 
                        (current_user.school_id, current_user.email, 'MoMo Payment (Sandbox)', f"Paid GHS {amount} for Fee ID {fee_id}"))
            conn.commit(); cur.close(); conn.close()
            return jsonify({"status": "cleared", "message": f"[SANDBOX MODE] Simulated USSD prompt approved for {phone}! GHS {amount} credited."}), 200
        except Exception as e:
            conn.rollback(); cur.close(); conn.close()
            return jsonify({"error": str(e)}), 500

# --- GES LESSON PLAN VAULT ---
@app.route('/api/lesson_plans', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_lesson_plans():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        if current_user.role != 'teacher': return jsonify({"error": "Teachers only"}), 403
        d = request.get_json()
        cur.execute("INSERT INTO lesson_plans (school_id, teacher_email, subject, class_name, week_number, topic, plan_content) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (current_user.school_id, current_user.email, d.get('subject'), d.get('class_name'), int(d.get('week_number') or 1), d.get('topic'), d.get('plan_content')))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Lesson Plan submitted for Headmaster approval!"}), 201

    elif request.method == 'PUT':
        if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
        d = request.get_json()
        cur.execute("UPDATE lesson_plans SET status = %s, admin_remarks = %s WHERE plan_id = %s AND school_id = %s",
                    (d.get('status'), d.get('remarks'), d.get('plan_id'), current_user.school_id))
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)",
                    (current_user.school_id, current_user.email, 'Reviewed Lesson Plan', f"Plan ID {d.get('plan_id')} ({d.get('status')})"))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Lesson plan review updated."}), 200

    else:
        if current_user.role == 'teacher':
            cur.execute("SELECT plan_id, subject, class_name, week_number, topic, status, admin_remarks, TO_CHAR(submitted_at, 'YYYY-MM-DD') as date FROM lesson_plans WHERE school_id = %s AND teacher_email = %s ORDER BY submitted_at DESC", (current_user.school_id, current_user.email))
        else:
            cur.execute("SELECT plan_id, teacher_email, subject, class_name, week_number, topic, plan_content, status, admin_remarks, TO_CHAR(submitted_at, 'YYYY-MM-DD') as date FROM lesson_plans WHERE school_id = %s ORDER BY submitted_at DESC", (current_user.school_id,))
        plans = cur.fetchall(); cur.close(); conn.close()
        return jsonify({"data": plans})

# --- CBT ASSESSMENT ENGINE ---
@app.route('/api/cbt/quiz', methods=['GET', 'POST'])
@login_required
def manage_cbt():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        if current_user.role not in ['admin', 'teacher']: return jsonify({"error": "Unauthorized"}), 403
        d = request.get_json()
        cur.execute("INSERT INTO cbt_quizzes (school_id, subject_name, class_name, title, academic_year, term) VALUES (%s, %s, %s, %s, %s, %s) RETURNING quiz_id",
                    (current_user.school_id, d.get('subject'), d.get('class_name'), d.get('title'), d.get('academic_year'), d.get('term')))
        quiz_id = cur.fetchone()['quiz_id']
        for q in d.get('questions', []):
            cur.execute("INSERT INTO cbt_questions (quiz_id, question_text, opt_a, opt_b, opt_c, opt_d, correct_opt) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (quiz_id, q.get('text'), q.get('a'), q.get('b'), q.get('c'), q.get('d'), q.get('ans').upper()))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "CBT Quiz published successfully!"}), 201
    else:
        cur.execute("SELECT quiz_id, subject_name, class_name, title, academic_year, term, TO_CHAR(created_at, 'YYYY-MM-DD') as date FROM cbt_quizzes WHERE school_id = %s ORDER BY created_at DESC", (current_user.school_id,))
        quizzes = cur.fetchall(); cur.close(); conn.close()
        return jsonify({"data": quizzes})

@app.route('/api/cbt/submit', methods=['POST'])
@login_required
def submit_cbt():
    d = request.get_json()
    quiz_id = int(d.get('quiz_id'))
    student_id = int(d.get('student_id'))
    answers = d.get('answers', {})
    
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT question_id, correct_opt FROM cbt_questions WHERE quiz_id = %s", (quiz_id,))
    questions = cur.fetchall()
    
    total = len(questions)
    if total == 0:
        cur.close(); conn.close(); return jsonify({"error": "Quiz has no questions."}), 400
        
    correct = 0
    for q in questions:
        q_id = str(q['question_id'])
        if answers.get(q_id, '').upper() == q['correct_opt']:
            correct += 1
            
    pct_score = int((correct / total) * 100)
    
    # Auto-grade write directly into SBA grades
    cur.execute("SELECT subject_name, academic_year, term FROM cbt_quizzes WHERE quiz_id = %s", (quiz_id,))
    q_meta = cur.fetchone()
    cur.execute("SELECT subject_id FROM subjects WHERE subject_name = %s AND school_id = %s", (q_meta['subject_name'], current_user.school_id))
    sub = cur.fetchone()
    sub_id = sub['subject_id'] if sub else None
    
    if sub_id:
        waec = get_waec_grade(pct_score)
        cur.execute("INSERT INTO grades (school_id, student_id, subject_id, class_score, exam_score, total_score, waec_grade, academic_year, term, teacher_remarks) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (current_user.school_id, student_id, sub_id, pct_score, 0, pct_score, waec, q_meta['academic_year'], q_meta['term'], 'CBT Online Assessment Score'))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": f"Assessment complete! You scored {correct}/{total} ({pct_score}%). Synchronized to SBA!"}), 200

# --- DIGITAL ALUMNI & TRANSCRIPT ENGINE ---
@app.route('/print_transcript/<int:student_id>', methods=['GET'])
@login_required
def print_transcript(student_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
    student = cur.fetchone()
    if not student: return "Student record not found.", 404
    
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone(); primary_color = inst['primary_color'] or '#0f4c81'
    
    cur.execute("""
        SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.academic_year, g.term 
        FROM grades g 
        JOIN subjects sub ON g.subject_id = sub.subject_id 
        WHERE g.student_id = %s AND g.school_id = %s 
        ORDER BY g.academic_year ASC, g.term ASC, sub.subject_name ASC
    """, (student_id, current_user.school_id))
    records = cur.fetchall(); cur.close(); conn.close()
    
    rows = ""
    for r in records:
        rows += f"<tr><td>{r['academic_year']}</td><td>{r['term']}</td><td>{r['subject_name']}</td><td>{r['class_score']}</td><td>{r['exam_score']}</td><td><strong>{r['total_score']}%</strong></td><td><strong>{r['waec_grade']}</strong></td></tr>"
        
    html = f"""<!DOCTYPE html><html><head><title>Official Transcript | {student['first_name']} {student['last_name']}</title>
    <style>
        :root {{ --primary: {primary_color}; }}
        body {{ font-family: 'Times New Roman', Times, serif; background: #eee; padding: 20px; }}
        .sheet {{ background: white; max-width: 900px; margin: auto; padding: 50px; border: 3px double #333; position: relative; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
        .header {{ text-align: center; border-bottom: 2px solid #333; padding-bottom: 20px; margin-bottom: 30px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th, td {{ border: 1px solid #333; padding: 8px 12px; font-size: 13px; text-align: left; }}
        th {{ background: #f2f2f2; text-transform: uppercase; }}
        .stamp-box {{ margin-top: 60px; display: flex; justify-content: space-between; }}
        .stamp {{ border-top: 1px solid #333; width: 220px; text-align: center; padding-top: 5px; font-weight: bold; font-size: 12px; }}
    </style></head><body>
    <div style="text-align:center; margin-bottom:20px;"><button onclick="window.print()" style="padding:10px 20px; font-weight:bold; cursor:pointer;">🖨️ Print Transcript</button></div>
    <div class="sheet">
        <div class="header">
            <h1 style="margin:0; text-transform:uppercase; font-size:26px;">{inst['school_name']}</h1>
            <p style="margin:5px 0;">{inst['address']} | Tel: {inst['phone']}</p>
            <h2 style="margin-top:15px; font-size:18px; text-decoration:underline;">OFFICIAL ACADEMIC TRANSCRIPT</h2>
        </div>
        <p><strong>Candidate:</strong> {student['first_name'].upper()} {student['last_name'].upper()} &nbsp;&nbsp;|&nbsp;&nbsp; <strong>Student ID:</strong> {inst['school_name'][:3].upper()}-{student['student_id']:04d} &nbsp;&nbsp;|&nbsp;&nbsp; <strong>Class:</strong> {student['current_class']}</p>
        <table>
            <tr><th>Academic Year</th><th>Term</th><th>Subject</th><th>Class (30)</th><th>Exam (70)</th><th>Total</th><th>Grade</th></tr>
            {rows}
        </table>
        <div class="stamp-box">
            <div class="stamp">Academic Registrar</div>
            <div class="stamp">Head of Institution / Stamp</div>
        </div>
    </div></body></html>"""
    return html

# --- ROUTE HANDLERS FOR REMAINING OPERATIONAL MODULES ---
@app.route('/api/transport', methods=['GET', 'POST'])
@login_required
def manage_transport():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
        d = request.get_json()
        cur.execute("INSERT INTO transport_routes (school_id, route_name, driver_name, fare) VALUES (%s, %s, %s, %s)", (current_user.school_id, d.get('route_name'), d.get('driver_name'), d.get('fare')))
        conn.commit(); cur.close(); conn.close(); return jsonify({"message": "Transport Route added!"}), 201
    else:
        cur.execute("SELECT route_id, route_name, driver_name, fare FROM transport_routes WHERE school_id = %s", (current_user.school_id,))
        routes = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": routes})

@app.route('/api/inventory', methods=['GET', 'POST'])
@login_required
def manage_inventory():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
        d = request.get_json()
        cur.execute("INSERT INTO inventory_items (school_id, item_name, price, stock) VALUES (%s, %s, %s, %s)", (current_user.school_id, d.get('item_name'), d.get('price'), d.get('stock')))
        conn.commit(); cur.close(); conn.close(); return jsonify({"message": "Item added to Store Catalog!"}), 201
    else:
        cur.execute("SELECT item_id, item_name, price, stock FROM inventory_items WHERE school_id = %s", (current_user.school_id,))
        items = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": items})

@app.route('/api/inventory/sell', methods=['POST'])
@login_required
def sell_inventory():
    if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
    d = request.get_json(); item_id = int(d.get('item_id')); qty = int(d.get('quantity')); stu_id = int(d.get('student_id'))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT item_name, price, stock FROM inventory_items WHERE item_id = %s AND school_id = %s", (item_id, current_user.school_id))
        item = cur.fetchone()
        if not item or item['stock'] < qty: return jsonify({"error": "Insufficient stock!"}), 400
        total = float(item['price']) * qty
        cur.execute("UPDATE inventory_items SET stock = stock - %s WHERE item_id = %s", (qty, item_id))
        cur.execute("INSERT INTO inventory_sales (school_id, student_id, item_name, quantity, total_cost) VALUES (%s, %s, %s, %s, %s)", (current_user.school_id, stu_id, item['item_name'], qty, total))
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Store POS Sale', f"Sold {qty} {item['item_name']} to ID {stu_id}"))
        conn.commit(); return jsonify({"message": f"Sale Successful! Total: GHS {total}"}), 200
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/exeats', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_exeats():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
        d = request.get_json(); stu_id = int(d.get('student_id'))
        try:
            cur.execute("SELECT first_name, guardian_contact FROM students WHERE student_id = %s AND school_id = %s", (stu_id, current_user.school_id))
            stu = cur.fetchone()
            if not stu: return jsonify({"error": "Student ID not found."}), 404
            cur.execute("INSERT INTO exeats (school_id, student_id, exeat_type, reason, expected_return) VALUES (%s, %s, %s, %s, %s)", (current_user.school_id, stu_id, d.get('exeat_type'), d.get('reason'), d.get('expected_return')))
            cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Issued Exeat', f"To ID {stu_id} for {d.get('reason')}"))
            conn.commit()
            return jsonify({"message": "Exeat issued successfully."}), 201
        except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
        finally: cur.close(); conn.close()
    elif request.method == 'PUT':
        if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
        d = request.get_json()
        cur.execute("UPDATE exeats SET status = 'Returned' WHERE exeat_id = %s AND school_id = %s", (d.get('exeat_id'), current_user.school_id))
        conn.commit(); cur.close(); conn.close(); return jsonify({"message": "Exeat marked as Returned."})
    else:
        cur.execute("SELECT e.exeat_id, e.student_id, s.first_name, s.last_name, e.exeat_type, e.reason, TO_CHAR(e.expected_return, 'YYYY-MM-DD') as date, e.status FROM exeats e JOIN students s ON e.student_id = s.student_id WHERE e.school_id = %s ORDER BY e.issue_date DESC", (current_user.school_id,))
        exs = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": exs})

@app.route('/api/sickbay', methods=['GET', 'POST'])
@login_required
def manage_sickbay():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
        d = request.get_json(); stu_id = int(d.get('student_id'))
        try:
            cur.execute("INSERT INTO sick_bay_logs (school_id, student_id, symptoms, treatment) VALUES (%s, %s, %s, %s)", (current_user.school_id, stu_id, d.get('symptoms'), d.get('treatment')))
            cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Sick Bay Log', f"ID {stu_id}: {d.get('symptoms')}"))
            conn.commit(); return jsonify({"message": "Medical visit recorded."}), 201
        except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
        finally: cur.close(); conn.close()
    else:
        cur.execute("SELECT sb.log_id, sb.student_id, s.first_name, s.last_name, sb.symptoms, sb.treatment, TO_CHAR(sb.log_date, 'YYYY-MM-DD HH24:MI') as time FROM sick_bay_logs sb JOIN students s ON sb.student_id = s.student_id WHERE sb.school_id = %s ORDER BY sb.log_date DESC", (current_user.school_id,))
        logs = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": logs})

@app.route('/api/calendar', methods=['GET', 'POST'])
@login_required
def manage_calendar():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
        d = request.get_json()
        cur.execute("INSERT INTO academic_calendar (school_id, event_title, event_date, description) VALUES (%s, %s, %s, %s)", (current_user.school_id, d.get('title'), d.get('date'), d.get('desc')))
        conn.commit(); cur.close(); conn.close(); return jsonify({"message": "Calendar event added!"}), 201
    else:
        cur.execute("SELECT event_title, TO_CHAR(event_date, 'YYYY-MM-DD') as date, description FROM academic_calendar WHERE school_id = %s ORDER BY event_date ASC", (current_user.school_id,))
        evs = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": evs})

@app.route('/api/students', methods=['GET', 'POST'])
@login_required
@require_active_subscription
def manage_students():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        data = request.get_json(); photo_b64 = data.get('photo_b64'); photo_key = None
        if photo_b64 and AWS_BUCKET_NAME:
            try:
                if ',' in photo_b64: photo_b64 = photo_b64.split(',')[1]
                photo_key = f"student_photos/{current_user.school_id}/{uuid.uuid4().hex}.jpg"
                s3_client.put_object(Bucket=AWS_BUCKET_NAME, Key=photo_key, Body=base64.b64decode(photo_b64), ContentType='image/jpeg')
            except Exception: pass
        cur.execute("INSERT INTO students (school_id, first_name, last_name, current_class, transport_route, guardian_name, guardian_contact, boarding_status, house, photo_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING student_id", 
                    (current_user.school_id, data.get('first_name'), data.get('last_name'), data.get('current_class', 'Unassigned'), data.get('transport_route', 'None'), data.get('guardian_name'), data.get('guardian_contact'), data.get('boarding_status', 'Day'), data.get('house', 'Unassigned'), photo_key))
        new_id = cur.fetchone()['student_id']
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Enrolled Student', f"ID {new_id}"))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": f"Student Enrolled successfully! ID: {new_id}"}), 201
    else:
        cur.execute("SELECT student_id, first_name, last_name, current_class, transport_route, boarding_status, house, guardian_contact FROM students WHERE school_id = %s ORDER BY student_id DESC", (current_user.school_id,))
        students = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": students})

@app.route('/api/students/<int:student_id>', methods=['PUT', 'DELETE'])
@login_required
@require_active_subscription
def update_delete_student(student_id):
    if current_user.role != 'admin': return jsonify({"error": "Admin clearance required."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'PUT':
        d = request.get_json()
        try:
            cur.execute("""
                UPDATE students 
                SET first_name = %s, last_name = %s, current_class = %s, transport_route = %s, boarding_status = %s, house = %s, guardian_contact = %s 
                WHERE student_id = %s AND school_id = %s
            """, (d.get('first_name'), d.get('last_name'), d.get('current_class'), d.get('transport_route'), d.get('boarding_status'), d.get('house'), d.get('guardian_contact'), student_id, current_user.school_id))
            cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Edited Student Profile', f"ID {student_id}"))
            conn.commit(); return jsonify({"message": f"Student ID {student_id} updated successfully."}), 200
        except Exception as e:
            conn.rollback(); return jsonify({"error": f"Update failed: {str(e)}"}), 500
        finally: cur.close(); conn.close()
    elif request.method == 'DELETE':
        try:
            cur.execute("SELECT photo_key FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
            student = cur.fetchone()
            if not student: return jsonify({"error": "Student record not found."}), 404
            cur.execute("DELETE FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
            cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Deleted Student Record', f"ID {student_id}"))
            conn.commit()
            if student['photo_key'] and AWS_BUCKET_NAME:
                try: s3_client.delete_object(Bucket=AWS_BUCKET_NAME, Key=student['photo_key'])
                except: pass
            return jsonify({"message": f"Student ID {student_id} permanently deleted."}), 200
        except Exception as e:
            conn.rollback(); return jsonify({"error": f"Database Error: {str(e)}"}), 500
        finally: cur.close(); conn.close()

@app.route('/print_ids', methods=['GET'])
@login_required
def print_ids():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT student_id, first_name, last_name, current_class, boarding_status, house FROM students WHERE school_id = %s ORDER BY student_id", (current_user.school_id,))
    students = cur.fetchall()
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone(); cur.close(); conn.close()
    primary_color = inst['primary_color'] or '#0f4c81'
    
    html = f"""<!DOCTYPE html><html><head><title>Print IDs</title><style>
        body {{ font-family:Arial; background:#f0f0f0; padding:20px; }}
        .page {{ display:grid; grid-template-columns:repeat(2,1fr); gap:15px; max-width:800px; margin:auto; }}
        .id-card {{ background:white; border:2px solid {primary_color}; border-radius:8px; padding:15px; width:350px; height:200px; box-sizing:border-box; position:relative; overflow:hidden; }}
        .header {{ background:{primary_color}; color:white; text-align:center; padding:5px; margin:-15px -15px 10px -15px; border-radius:6px 6px 0 0; font-weight:bold; font-size:14px; }}
        .photo-box {{ width:70px; height:90px; border:1px solid #ccc; float:left; margin-right:12px; background:#eee; }}
        .details {{ float:left; font-size:11px; line-height:1.5; width:150px; }}
        .qr-box {{ float:right; width:70px; height:70px; }}
        .footer {{ position:absolute; bottom:0; left:0; width:100%; background:#eee; text-align:center; font-size:9px; padding:4px 0; font-weight:bold; }}
        @media print {{ body {{ background:white; padding:0; }} .no-print {{ display:none; }} }}
    </style></head><body>
    <button class='no-print' onclick='window.print()' style='padding:10px; margin-bottom:20px; cursor:pointer;'>🖨️ Print IDs</button>
    <div class='page'>"""
    
    for s in students:
        c_name = s.get('current_class') or 'N/A'
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=70x70&data=VERIFIED:{inst['school_name']}:STU-{s['student_id']}"
        html += f"""<div class='id-card'>
            <div class='header'>{inst['school_name']}</div>
            <div class='photo-box'><img src='/api/photo/{s['student_id']}' style='width:100%; height:100%; object-fit:cover;'></div>
            <div class='details'>
                <strong>Name:</strong> {s['first_name']} {s['last_name']}<br>
                <strong>ID:</strong> {inst['school_name'][:3].upper()}-{s['student_id']:04d}<br>
                <strong>Class:</strong> {c_name}<br>
                <strong>Status:</strong> {s['boarding_status']}
            </div>
            <div class='qr-box'><img src='{qr_url}' style='width:100%; height:100%;'></div>
            <div class='footer'>CONTACT: {inst['phone']} | {inst['address']}</div>
        </div>"""
    html += "</div></body></html>"
    return html

# --- PASS-THROUGH LOGIC FOR REMAINING BILLING, GRADES & EXPENSES ---
@app.route('/api/fees/bill', methods=['POST'])
@login_required
def bill_student():
    d = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO fees (school_id, student_id, fee_category, description, amount_due, academic_year, term) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (current_user.school_id, int(d.get('student_id')), d.get('fee_category'), d.get('description'), float(d.get('amount_due')), d.get('academic_year'), d.get('term')))
        conn.commit(); return jsonify({"message": "Bill issued successfully!"}), 201
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 400
    finally: cur.close(); conn.close()

@app.route('/api/statement/<int:student_id>', methods=['GET'])
@login_required
def get_statement(student_id):
    conn = get_db_connection(); cur = conn.cursor()
    query = "SELECT f.fee_id, f.student_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due, COALESCE(SUM(p.amount_paid), 0) as total_paid, (f.amount_due - COALESCE(SUM(p.amount_paid), 0)) as remaining_balance FROM fees f LEFT JOIN payments p ON f.fee_id = p.fee_id WHERE f.student_id = %s AND f.school_id = %s GROUP BY f.fee_id, f.student_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due ORDER BY f.date_issued DESC"
    cur.execute(query, (student_id, current_user.school_id))
    statement = cur.fetchall(); cur.close(); conn.close(); return jsonify({"statement": statement}), 200

# --- FRONTEND CLIENT INTERFACE ---
@app.route('/dashboard')
def dashboard():
    school_name = "Global ERP Engine"; primary_color = "#0f4c81"
    if current_user.is_authenticated and current_user.school_id:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT school_name, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
        inst = cur.fetchone(); cur.close(); conn.close()
        if inst: school_name = inst['school_name']; primary_color = inst['primary_color'] or "#0f4c81"

    html_template = """<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><title>{{ school_name }} | ERP Portal</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root { --primary: {{ primary_color }}; --secondary: #f4f7fa; --accent: #28a745; --danger: #dc3545; --info: #17a2b8; --warning: #ffc107; }
            body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--secondary); margin: 0; display: flex; color: #333; }
            .sidebar { width: 260px; background: var(--primary); color: white; min-height: 100vh; padding: 20px; box-sizing: border-box; position: fixed; overflow-y: auto; }
            .sidebar button { background: transparent; color: rgba(255,255,255,0.85); border: none; padding: 10px 14px; width: 100%; text-align: left; margin-bottom: 5px; border-radius: 6px; cursor: pointer; font-size: 0.95rem; font-weight: 500; }
            .sidebar button:hover { background: rgba(255,255,255,0.15); color: white; }
            .main-content { margin-left: 260px; flex: 1; padding: 35px; box-sizing: border-box; min-height: 100vh; }
            .card { background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.04); margin-bottom: 25px; }
            h3 { margin-top: 0; color: var(--primary); border-bottom: 2px solid #f0f0f0; padding-bottom: 8px; }
            input, select, textarea { width: 100%; padding: 10px; margin-bottom: 12px; border: 1px solid #ced4da; border-radius: 6px; box-sizing: border-box; }
            .btn { background: var(--primary); color: white; border: none; padding: 10px 16px; border-radius: 6px; cursor: pointer; font-weight: bold; width: 100%; margin-bottom: 8px; }
            .btn-success { background: var(--accent); } .btn-danger { background: var(--danger); } .btn-info { background: var(--info); } .btn-warning { background: var(--warning); color: #333;}
            .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            #toast { display: none; position: fixed; bottom: 30px; right: 30px; padding: 15px 25px; color: white; background: var(--accent); border-radius: 8px; z-index: 1000; font-weight: bold; }
            .hidden { display: none !important; }
        </style>
    </head>
    <body>
        <div id="toast">Message</div>
        <div class="sidebar">
            <h2 style="font-size: 1.1rem; margin-top:0;">{{ school_name }}</h2>
            {% if current_user.is_authenticated %}
                <div style="font-size:0.8rem; color:rgba(255,255,255,0.6); margin-bottom:15px;">ROLE: {{ current_user.role|upper }}</div>
                {% if current_user.role == 'superadmin' %}
                    <button onclick="window.location.reload()">🏢 Global Tenants</button>
                    <button onclick="showSection('sa-settings-section')">⚙️ Master Settings</button>
                {% elif current_user.role == 'admin' %}
                    <button onclick="showSection('admissions-section')">🎓 Admissions & IDs</button>
                    <button onclick="showSection('finance-section')">💰 Financials & MoMo</button>
                    <button onclick="showSection('transport-section')">🚌 Transport Logistics</button>
                    <button onclick="showSection('store-section')">📦 Store & Uniforms</button>
                    <button onclick="showSection('exeat-section')">🎫 Exeat Desk</button>
                    <button onclick="showSection('sickbay-section')">🏥 Sick Bay Log</button>
                    <button onclick="showSection('lesson-section')">📚 GES Lesson Plans</button>
                    <button onclick="showSection('cbt-section')">💻 CBT Testing</button>
                    <button onclick="showSection('settings-section')">⚙️ Settings</button>
                {% elif current_user.role == 'teacher' %}
                    <button onclick="showSection('lesson-section')">📚 Submit Lesson Notes</button>
                    <button onclick="showSection('cbt-section')">💻 CBT Assessment</button>
                {% elif current_user.role == 'guardian' %}
                    <button onclick="showSection('guardian-section')">👨‍👩‍👧 Guardian Portal</button>
                {% endif %}
                <button class="btn-danger" style="margin-top:30px;" onclick="logout()">🛑 Logout</button>
            {% endif %}
        </div>

        <main class="main-content">
            {% if not current_user.is_authenticated %}
            <div class="card" style="max-width: 400px; margin: 60px auto;">
                <h3>System Login</h3>
                <input type="email" id="email" placeholder="Email Address">
                <input type="password" id="pass" placeholder="Password">
                <button class="btn" onclick="login()">Sign In</button>
            </div>
            {% elif current_user.role == 'guardian' %}
            <div id="guardian-section" class="card">
                <h3>Guardian Portal</h3>
                <div class="grid-2">
                    <button class="btn btn-success" onclick="window.open('/print_report/{{ current_user.linked_student_id }}', '_blank')">🖨️ Terminal Report Card</button>
                    <button class="btn btn-info" onclick="window.open('/print_transcript/{{ current_user.linked_student_id }}', '_blank')">📜 Full Official Transcript</button>
                </div>
                <hr style="margin:20px 0; border:1px solid #eee;">
                <h4>Pay Fees via Mobile Money</h4>
                <div class="grid-2">
                    <input type="number" id="momoFeeId" placeholder="Target Fee ID">
                    <input type="number" id="momoAmount" placeholder="Amount (GHS)">
                </div>
                <input type="text" id="momoPhone" placeholder="MoMo Phone Number (e.g. 0244123456)">
                <button class="btn btn-warning" onclick="initiateMoMo()">Authorize MoMo Payment</button>
            </div>
            {% elif current_user.role == 'admin' %}
            
            <!-- GES LESSON PLAN REVIEW SECTION -->
            <div id="lesson-section" class="card hidden">
                <h3>📚 GES Lesson Plan Inspection Vault</h3>
                <button class="btn btn-info" style="width:auto; margin-bottom:15px;" onclick="loadLessonPlans()">Refresh Submitted Notes</button>
                <div id="plan-container"></div>
            </div>

            <!-- CBT SECTION -->
            <div id="cbt-section" class="card hidden">
                <h3>💻 Computer-Based Testing (CBT) Engine</h3>
                <div style="background:#f8f9fa; padding:15px; border-radius:8px; margin-bottom:20px;">
                    <h4>Publish New Multiple-Choice Test</h4>
                    <div class="grid-2">
                        <input type="text" id="cbtSubject" placeholder="Subject">
                        <input type="text" id="cbtClass" placeholder="Class">
                    </div>
                    <input type="text" id="cbtTitle" placeholder="Assessment Title (e.g. Midterm Objective Quiz)">
                    <div class="grid-2">
                        <input type="text" id="cbtYear" placeholder="Academic Year (e.g. 2026)">
                        <input type="text" id="cbtTerm" placeholder="Term">
                    </div>
                    <textarea id="cbtQText" placeholder="Sample Question: What is the capital of Ghana?"></textarea>
                    <div class="grid-2">
                        <input type="text" id="cbtOptA" placeholder="Option A: Kumasi">
                        <input type="text" id="cbtOptB" placeholder="Option B: Accra">
                        <input type="text" id="cbtOptC" placeholder="Option C: Cape Coast">
                        <input type="text" id="cbtOptD" placeholder="Option D: Takoradi">
                    </div>
                    <input type="text" id="cbtCorrect" placeholder="Correct Option Letter (A, B, C, or D)">
                    <button class="btn btn-success" onclick="publishQuiz()">Publish Test & Synchronize</button>
                </div>
            </div>

            <!-- ADMISSIONS WITH TRANSCRIPT ENGINE -->
            <div id="admissions-section" class="card">
                <h3>🎓 Admissions & Student Tools</h3>
                <div class="grid-2">
                    <div>
                        <input type="number" id="trStuId" placeholder="Student ID for Transcript">
                        <button class="btn btn-info" onclick="generateTranscript()">📜 Generate Official Transcript</button>
                    </div>
                    <div>
                        <button class="btn btn-warning" onclick="window.open('/print_ids', '_blank')">🖨️ Batch Student IDs (with QR)</button>
                    </div>
                </div>
            </div>

            <div id="finance-section" class="card hidden">
                <h3>💰 Financials & MoMo Gateway</h3>
                <div class="grid-2">
                    <input type="number" id="bStuId" placeholder="Student ID">
                    <input type="number" id="bAmount" placeholder="Amount Due (GHS)">
                </div>
                <button class="btn btn-success" onclick="sendAction('/api/fees/bill', {student_id: document.getElementById('bStuId').value, amount_due: document.getElementById('bAmount').value, fee_category: 'Tuition', academic_year: '2026', term: 'Term 1', description: 'Academic Bill'})">Issue Bill</button>
            </div>
            
            <div id="transport-section" class="card hidden">
                <h3>🚌 Bus Routes</h3>
                <input type="text" id="trName" placeholder="Route Name">
                <input type="text" id="trDriver" placeholder="Driver">
                <input type="number" id="trFare" placeholder="Fare (GHS)">
                <button class="btn" onclick="sendAction('/api/transport', {route_name: document.getElementById('trName').value, driver_name: document.getElementById('trDriver').value, fare: document.getElementById('trFare').value})">Save Route</button>
            </div>

            <div id="store-section" class="card hidden">
                <h3>📦 Store POS</h3>
                <input type="text" id="invName" placeholder="Item Name">
                <input type="number" id="invPrice" placeholder="Price">
                <input type="number" id="invStock" placeholder="Stock Qty">
                <button class="btn" onclick="sendAction('/api/inventory', {item_name: document.getElementById('invName').value, price: document.getElementById('invPrice').value, stock: document.getElementById('invStock').value})">Add Item</button>
            </div>

            <div id="exeat-section" class="card hidden">
                <h3>🎫 Exeat Permission</h3>
                <input type="number" id="exStuId" placeholder="Student ID">
                <input type="text" id="exReason" placeholder="Reason">
                <input type="date" id="exReturn">
                <button class="btn btn-warning" onclick="sendAction('/api/exeats', {student_id: document.getElementById('exStuId').value, exeat_type: 'Weekend', reason: document.getElementById('exReason').value, expected_return: document.getElementById('exReturn').value})">Issue Exeat</button>
            </div>

            <div id="sickbay-section" class="card hidden">
                <h3>🏥 Sick Bay Entry</h3>
                <input type="number" id="sbStuId" placeholder="Student ID">
                <input type="text" id="sbSymp" placeholder="Symptoms">
                <input type="text" id="sbTreat" placeholder="Treatment">
                <button class="btn btn-danger" onclick="sendAction('/api/sickbay', {student_id: document.getElementById('sbStuId').value, symptoms: document.getElementById('sbSymp').value, treatment: document.getElementById('sbTreat').value})">Log Treatment</button>
            </div>
            {% endif %}
        </main>

        <script>
            function showToast(msg) {
                const t = document.getElementById('toast'); t.innerText = msg; t.style.display = 'block';
                setTimeout(() => t.style.display = 'none', 4000);
            }
            function showSection(id) {
                document.querySelectorAll('.card').forEach(c => { if(c.id) c.classList.add('hidden'); });
                const target = document.getElementById(id); if(target) target.classList.remove('hidden');
            }
            async function login() {
                const res = await fetch('/api/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email:document.getElementById('email').value, password:document.getElementById('pass').value}) });
                const d = await res.json();
                if(res.ok) window.location.reload(); else showToast(d.error);
            }
            async function logout() { await fetch('/api/logout', {method:'POST'}); window.location.reload(); }
            async function sendAction(endpoint, payload) {
                const res = await fetch(endpoint, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
                const d = await res.json(); showToast(d.message || d.error);
            }
            async function initiateMoMo() {
                const res = await fetch('/api/momo/initialize', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({fee_id: document.getElementById('momoFeeId').value, amount: document.getElementById('momoAmount').value, phone: document.getElementById('momoPhone').value})
                });
                const d = await res.json();
                if(d.status === 'paystack_redirect') window.location.href = d.auth_url;
                else showToast(d.message || d.error);
            }
            function generateTranscript() {
                const id = document.getElementById('trStuId').value;
                if(!id) return showToast("Enter Student ID");
                window.open('/print_transcript/' + id, '_blank');
            }
            async function publishQuiz() {
                const payload = {
                    subject: document.getElementById('cbtSubject').value,
                    class_name: document.getElementById('cbtClass').value,
                    title: document.getElementById('cbtTitle').value,
                    academic_year: document.getElementById('cbtYear').value,
                    term: document.getElementById('cbtTerm').value,
                    questions: [{
                        text: document.getElementById('cbtQText').value,
                        a: document.getElementById('cbtOptA').value,
                        b: document.getElementById('cbtOptB').value,
                        c: document.getElementById('cbtOptC').value,
                        d: document.getElementById('cbtOptD').value,
                        ans: document.getElementById('cbtCorrect').value
                    }]
                };
                sendAction('/api/cbt/quiz', payload);
            }
            async function loadLessonPlans() {
                const res = await fetch('/api/lesson_plans'); const d = await res.json();
                let html = '<table style="width:100%; border-collapse:collapse;"><tr><th>Teacher</th><th>Subject</th><th>Topic</th><th>Status</th><th>Action</th></tr>';
                d.data.forEach(p => {
                    html += `<tr><td>${p.teacher_email}</td><td>${p.subject}</td><td>${p.topic}</td><td><b>${p.status}</b></td><td><button onclick="reviewPlan(${p.plan_id}, 'Approved')">Approve</button></td></tr>`;
                });
                document.getElementById('plan-container').innerHTML = html + '</table>';
            }
            async function reviewPlan(id, status) {
                const res = await fetch('/api/lesson_plans', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({plan_id: id, status: status, remarks:'Inspected and approved by Headmaster.'}) });
                const d = await res.json(); showToast(d.message); loadLessonPlans();
            }
        </script>
    </body>
    </html>"""
    return render_template_string(html_template, current_user=current_user, school_name=school_name, primary_color=primary_color)

# --- AUTOMATIC BOOT SEQUENCE ---
with app.app_context():
    try:
        initialize_database()
        print("SaaS Database synchronized successfully on boot.")
    except Exception as e:
        print(f"Boot synchronization skipped: {e}")

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
