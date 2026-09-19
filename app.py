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
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id, email, role, linked_student_id, school_id FROM system_users WHERE user_id = %s", (user_id,))
        user_data = cur.fetchone()
        cur.close(); conn.close()
        if user_data:
            return User(user_data['user_id'], user_data['email'], user_data['role'], user_data['linked_student_id'], user_data['school_id'])
        return None

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

def require_active_subscription(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.role == 'superadmin': return f(*args, **kwargs)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT subscription_expiry_date FROM institutions WHERE school_id = %s", (current_user.school_id,))
        school = cur.fetchone()
        cur.close(); conn.close()
        if not school or school['subscription_expiry_date'] < datetime.now().date():
            return jsonify({"error": "ACCESS LOCKED: Your annual subscription has expired."}), 402 
        return f(*args, **kwargs)
    return decorated_function

# --- CORE DATABASE ENGINE ---
def initialize_database():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS institutions (school_id SERIAL PRIMARY KEY, school_name VARCHAR(150) NOT NULL UNIQUE, subscription_expiry_date DATE NOT NULL)")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS address VARCHAR(255) DEFAULT 'Ghana'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS phone VARCHAR(50) DEFAULT '0000000000'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS primary_color VARCHAR(20) DEFAULT '#0f4c81'")
    cur.execute("ALTER TABLE institutions ADD COLUMN IF NOT EXISTS logo_key VARCHAR(255)")
    
    cur.execute("CREATE TABLE IF NOT EXISTS students (student_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, first_name VARCHAR(100) NOT NULL, last_name VARCHAR(100) NOT NULL, guardian_name VARCHAR(100) NOT NULL, guardian_contact VARCHAR(20) NOT NULL, enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS boarding_status VARCHAR(20) DEFAULT 'Day'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS house VARCHAR(100) DEFAULT 'Unassigned'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS current_class VARCHAR(100) DEFAULT 'Unassigned'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS photo_key VARCHAR(255)")
    
    cur.execute("CREATE TABLE IF NOT EXISTS system_users (user_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, email VARCHAR(100) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, linked_student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE)")
    cur.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS phone VARCHAR(20)")
    cur.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS subject VARCHAR(100)")
    cur.execute("ALTER TABLE system_users ADD COLUMN IF NOT EXISTS base_salary DECIMAL(10,2) DEFAULT 0.00")
    
    # Security Audit Logs Table
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
    
    cur.execute("SELECT * FROM system_users WHERE role = 'superadmin'")
    if not cur.fetchone():
        hashed_sa = generate_password_hash('ceo123')
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s)", ('superadmin@engine.com', hashed_sa, 'superadmin'))
    conn.commit()
    cur.close()
    conn.close()

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
def logout():
    logout_user(); return jsonify({"message": "Logged out safely."})

@app.route('/api/verify_password', methods=['POST'])
@login_required
def verify_password():
    """Secondary authentication lock for sensitive tabs"""
    d = request.get_json()
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT password_hash FROM system_users WHERE user_id = %s", (current_user.id,))
    user = cur.fetchone(); cur.close(); conn.close()
    if user and check_password_hash(user['password_hash'], d.get('password')):
        return jsonify({"message": "Vault unlocked."}), 200
    return jsonify({"error": "Incorrect password. Access denied."}), 403

@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    d = request.get_json()
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("SELECT password_hash FROM system_users WHERE user_id = %s", (current_user.id,))
        user = cur.fetchone()
        if not check_password_hash(user['password_hash'], d.get('current_password')):
            return jsonify({"error": "Incorrect current password."}), 403
        
        new_hash = generate_password_hash(d.get('new_password'))
        cur.execute("UPDATE system_users SET password_hash = %s WHERE user_id = %s", (new_hash, current_user.id))
        conn.commit(); return jsonify({"message": "Your private password has been successfully updated!"}), 200
    except Exception as e:
        conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/admin/reset_password', methods=['POST'])
@login_required
def admin_reset_password():
    if current_user.role != 'admin': return jsonify({"error": "Admin clearance required."}), 403
    d = request.get_json()
    new_hash = generate_password_hash(d.get('new_password'))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE system_users SET password_hash = %s WHERE email = %s AND school_id = %s RETURNING user_id", 
                    (new_hash, d.get('target_email'), current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": "User email not found in your school's database."}), 404
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Forced Password Reset', f"Target Email: {d.get('target_email')}"))
        conn.commit(); return jsonify({"message": f"Security override successful. Password reset for {d.get('target_email')}!"}), 200
    except Exception as e:
        conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/superadmin/reset_admin', methods=['POST'])
@login_required
def superadmin_reset_admin():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    d = request.get_json()
    new_hash = generate_password_hash(d.get('new_password'))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE system_users SET password_hash = %s WHERE email = %s RETURNING user_id", (new_hash, d.get('admin_email')))
        if not cur.fetchone(): return jsonify({"error": "Admin email not found in global registry."}), 404
        conn.commit(); return jsonify({"message": f"Global Override successful. Password reset for {d.get('admin_email')}!"}), 200
    except Exception as e:
        conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/superadmin/credentials', methods=['POST'])
@login_required
def superadmin_update_credentials():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    d = request.get_json()
    new_email = d.get('new_email')
    new_pass = d.get('new_password')
    conn = get_db_connection(); cur = conn.cursor()
    try:
        if new_email:
            cur.execute("UPDATE system_users SET email = %s WHERE user_id = %s", (new_email, current_user.id))
        if new_pass:
            new_hash = generate_password_hash(new_pass)
            cur.execute("UPDATE system_users SET password_hash = %s WHERE user_id = %s", (new_hash, current_user.id))
        conn.commit(); return jsonify({"message": "Master Super Admin credentials updated successfully!"}), 200
    except psycopg2.IntegrityError:
        conn.rollback(); return jsonify({"error": "That email is already in use by another user."}), 409
    except Exception as e:
        conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

# --- IMMUTABLE AUDIT LOGS ENDPOINT ---
@app.route('/api/audit_logs', methods=['GET'])
@login_required
def get_audit_logs():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS') as time, user_email, action, target FROM audit_logs WHERE school_id = %s ORDER BY timestamp DESC LIMIT 200", (current_user.school_id,))
    logs = cur.fetchall(); cur.close(); conn.close()
    return jsonify({"data": logs})

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
    d = request.get_json()
    color = d.get('color') or '#0f4c81'
    logo_b64 = d.get('logo_b64')
    
    conn = get_db_connection(); cur = conn.cursor()
    if logo_b64 and AWS_BUCKET_NAME:
        try:
            if ',' in logo_b64: logo_b64 = logo_b64.split(',')[1]
            logo_key = f"school_logos/{school_id}/{uuid.uuid4().hex}.png"
            s3_client.put_object(Bucket=AWS_BUCKET_NAME, Key=logo_key, Body=base64.b64decode(logo_b64), ContentType='image/png')
            cur.execute("UPDATE institutions SET primary_color = %s, logo_key = %s WHERE school_id = %s", (color, logo_key, school_id))
        except Exception: pass
    else:
        cur.execute("UPDATE institutions SET primary_color = %s WHERE school_id = %s", (color, school_id))
    
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": "School branding & colors updated successfully!"})

@app.route('/api/superadmin/renew/<int:school_id>', methods=['POST'])
@login_required
def renew_school(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE institutions SET subscription_expiry_date = CURRENT_DATE + INTERVAL '1 year' WHERE school_id = %s RETURNING school_name, subscription_expiry_date", (school_id,))
    updated = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return jsonify({"message": f"Contract Renewed! {updated['school_name']} active until {updated['subscription_expiry_date']}"})

@app.route('/api/superadmin/backup/<int:school_id>', methods=['GET'])
@login_required
def download_backup(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT school_name FROM institutions WHERE school_id = %s", (school_id,))
    school = cur.fetchone()
    if not school: return jsonify({"error": "School not found"}), 404
    backup = {"school_name": school['school_name'], "export_date": datetime.now().isoformat(), "data": {}}
    cur.execute("SELECT * FROM subjects WHERE school_id = %s", (school_id,)); backup['data']['subjects'] = cur.fetchall()
    cur.execute("SELECT * FROM students WHERE school_id = %s", (school_id,)); backup['data']['students'] = cur.fetchall()
    cur.execute("SELECT * FROM fees WHERE school_id = %s", (school_id,)); backup['data']['fees'] = cur.fetchall()
    cur.execute("SELECT p.* FROM payments p JOIN fees f ON p.fee_id = f.fee_id WHERE f.school_id = %s", (school_id,)); backup['data']['payments'] = cur.fetchall()
    cur.execute("SELECT * FROM grades WHERE school_id = %s", (school_id,)); backup['data']['grades'] = cur.fetchall()
    cur.execute("SELECT * FROM expenses WHERE school_id = %s", (school_id,)); backup['data']['expenses'] = cur.fetchall()
    cur.close(); conn.close()
    json_data = json.dumps(backup, default=custom_json_serializer, indent=4)
    return Response(json_data, mimetype="application/json", headers={"Content-Disposition": f"attachment;filename=Backup_{school['school_name'].replace(' ', '_')}.json"})

@app.route('/api/superadmin/restore/<int:school_id>', methods=['POST'])
@login_required
def restore_backup(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    if 'file' not in request.files: return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    try:
        data = json.load(file)
        conn = get_db_connection(); cur = conn.cursor()
        if 'subjects' in data['data']:
            for r in data['data']['subjects']:
                cur.execute("INSERT INTO subjects (subject_id, school_id, subject_name) VALUES (%s, %s, %s) ON CONFLICT (subject_id) DO NOTHING", (r['subject_id'], school_id, r['subject_name']))
        if 'students' in data['data']:
            for r in data['data']['students']:
                cur.execute("INSERT INTO students (student_id, school_id, first_name, last_name, current_class, guardian_name, guardian_contact, boarding_status, house, photo_key, enrollment_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (student_id) DO NOTHING", (r['student_id'], school_id, r['first_name'], r['last_name'], r.get('current_class', 'Unassigned'), r['guardian_name'], r['guardian_contact'], r.get('boarding_status', 'Day'), r.get('house', 'Unassigned'), r.get('photo_key'), r['enrollment_date']))
        if 'fees' in data['data']:
            for r in data['data']['fees']:
                cur.execute("INSERT INTO fees (fee_id, school_id, student_id, fee_category, description, amount_due, academic_year, term, date_issued) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (fee_id) DO NOTHING", (r['fee_id'], school_id, r['student_id'], r.get('fee_category', 'General'), r['description'], r['amount_due'], r.get('academic_year', 'Unknown'), r.get('term', 'Unknown'), r['date_issued']))
        if 'payments' in data['data']:
            for r in data['data']['payments']:
                cur.execute("INSERT INTO payments (payment_id, fee_id, amount_paid, payment_method, payment_date) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (payment_id) DO NOTHING", (r['payment_id'], r['fee_id'], r['amount_paid'], r['payment_method'], r['payment_date']))
        if 'grades' in data['data']:
            for r in data['data']['grades']:
                cur.execute("INSERT INTO grades (grade_id, school_id, student_id, subject_id, class_score, exam_score, total_score, waec_grade, academic_year, term, teacher_remarks) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (grade_id) DO NOTHING", (r['grade_id'], school_id, r['student_id'], r['subject_id'], r.get('class_score', 0), r.get('exam_score', r.get('score', 0)), r.get('total_score', r.get('score', 0)), r['waec_grade'], r['academic_year'], r['term'], r.get('teacher_remarks', '')))
        if 'expenses' in data['data']:
            for r in data['data']['expenses']:
                cur.execute("INSERT INTO expenses (expense_id, school_id, category, description, amount, date_incurred) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (expense_id) DO NOTHING", (r['expense_id'], school_id, r['category'], r['description'], r['amount'], r['date_incurred']))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": f"Vault Restoration Complete for {data.get('school_name')}!"}), 200
    except Exception as e: return jsonify({"error": f"Restoration failed: {str(e)}"}), 500

# --- TENANT ENDPOINTS (ENROLLMENT & DIRECTORY) ---
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
        cur.execute("INSERT INTO students (school_id, first_name, last_name, current_class, guardian_name, guardian_contact, boarding_status, house, photo_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING student_id", 
                    (current_user.school_id, data.get('first_name'), data.get('last_name'), data.get('current_class', 'Unassigned'), data.get('guardian_name'), data.get('guardian_contact'), data.get('boarding_status', 'Day'), data.get('house', 'Unassigned'), photo_key))
        new_id = cur.fetchone()['student_id']
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Enrolled Student', f"ID {new_id}"))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": f"Student Enrolled successfully! ID: {new_id}"}), 201
    else:
        cur.execute("SELECT student_id, first_name, last_name, current_class, boarding_status, house, guardian_contact FROM students WHERE school_id = %s ORDER BY student_id DESC", (current_user.school_id,))
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
                SET first_name = %s, last_name = %s, current_class = %s, boarding_status = %s, house = %s, guardian_contact = %s 
                WHERE student_id = %s AND school_id = %s
            """, (d.get('first_name'), d.get('last_name'), d.get('current_class'), d.get('boarding_status'), d.get('house'), d.get('guardian_contact'), student_id, current_user.school_id))
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

@app.route('/api/students/bulk', methods=['POST'])
@login_required
def bulk_enroll():
    if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
    if 'file' not in request.files: return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    try:
        stream = StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.reader(stream); next(csv_input, None) 
        conn = get_db_connection(); cur = conn.cursor()
        count = 0
        for row in csv_input:
            if len(row) >= 2: 
                fname = row[0].strip()[:100]
                lname = row[1].strip()[:100]
                c_class = row[2].strip()[:100] if len(row) > 2 and row[2].strip() else 'Unassigned'
                g_name = row[3].strip()[:100] if len(row) > 3 and row[3].strip() else 'N/A'
                g_contact = row[4].strip()[:20] if len(row) > 4 and row[4].strip() else ''
                b_status = row[5].strip()[:20] if len(row) > 5 and row[5].strip() else 'Day'
                house = row[6].strip()[:100] if len(row) > 6 and row[6].strip() else 'Unassigned'
                
                cur.execute("INSERT INTO students (school_id, first_name, last_name, current_class, guardian_name, guardian_contact, boarding_status, house) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            (current_user.school_id, fname, lname, c_class, g_name, g_contact, b_status, house))
                count += 1
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Bulk CSV Enrollment', f"Enrolled {count} students"))
        conn.commit(); return jsonify({"message": f"Bulk Upload Success! Enrolled {count} students."}), 201
    except Exception as e: return jsonify({"error": f"Upload failed. Ensure CSV format is correct. Error: {str(e)}"}), 500
    finally:
        if 'cur' in locals(): cur.close(); conn.close()

@app.route('/api/students/promote', methods=['POST'])
@login_required
def promote_students():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    d = request.get_json()
    from_class = str(d.get('from_class') or '').strip()
    to_class = str(d.get('to_class') or '').strip()
    if not from_class or not to_class: return jsonify({"error": "You must provide both Current and Next class names."}), 400
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE students SET current_class = %s WHERE current_class = %s AND school_id = %s RETURNING student_id", (to_class, from_class, current_user.school_id))
        promoted = cur.fetchall()
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Promoted Cohort', f"Moved {len(promoted)} from {from_class} to {to_class}"))
        conn.commit(); return jsonify({"message": f"Success! Promoted {len(promoted)} students from {from_class} to {to_class}."}), 200
    except Exception as e:
        conn.rollback(); return jsonify({"error": f"Database Error: {str(e)}"}), 500
    finally: cur.close(); conn.close()

@app.route('/api/photo/<int:student_id>', methods=['GET'])
def get_photo(student_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT photo_key FROM students WHERE student_id = %s", (student_id,))
    student = cur.fetchone(); cur.close(); conn.close()
    if student and student['photo_key'] and AWS_BUCKET_NAME:
        try:
            file_obj = s3_client.get_object(Bucket=AWS_BUCKET_NAME, Key=student['photo_key'])
            return Response(file_obj['Body'].read(), mimetype='image/jpeg')
        except Exception: pass
    return Response('<svg xmlns="http://www.w3.org/2000/svg" width="70" height="90"><rect width="70" height="90" fill="#eee"/><text x="15" y="50" font-family="Arial" font-size="12" fill="#999">PHOTO</text></svg>', mimetype='image/svg+xml')

@app.route('/api/logo/<int:school_id>', methods=['GET'])
def get_school_logo(school_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT logo_key FROM institutions WHERE school_id = %s", (school_id,))
    inst = cur.fetchone(); cur.close(); conn.close()
    if inst and inst['logo_key'] and AWS_BUCKET_NAME:
        try:
            file_obj = s3_client.get_object(Bucket=AWS_BUCKET_NAME, Key=inst['logo_key'])
            return Response(file_obj['Body'].read(), mimetype='image/png')
        except Exception: pass
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="#ffffff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="background:var(--primary); padding:10px; border-radius:12px;"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>'
    return Response(svg, mimetype='image/svg+xml')

@app.route('/print_ids', methods=['GET'])
@login_required
def print_ids():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT student_id, first_name, last_name, current_class, boarding_status, house FROM students WHERE school_id = %s ORDER BY student_id", (current_user.school_id,))
    students = cur.fetchall()
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone(); cur.close(); conn.close()
    primary_color = inst['primary_color'] or '#0f4c81'
    
    html = f"<!DOCTYPE html><html><head><title>Print IDs</title><style>body{{font-family:Arial;background:#f0f0f0;padding:20px;}}.page{{display:grid;grid-template-columns:repeat(2,1fr);gap:15px;max-width:800px;margin:auto;}}.id-card{{background:white;border:2px solid {primary_color};border-radius:8px;padding:15px;width:350px;height:200px;box-sizing:border-box;position:relative;overflow:hidden;}}.header{{background:{primary_color};color:white;text-align:center;padding:5px;margin:-15px -15px 10px -15px;border-radius:6px 6px 0 0;font-weight:bold;font-size:14px;}}.photo-box{{width:70px;height:90px;border:1px solid #ccc;float:left;margin-right:15px;background:#eee;}}.details{{float:left;font-size:12px;line-height:1.6;width:calc(100% - 90px);}}.footer{{position:absolute;bottom:0;left:0;width:100%;background:#eee;text-align:center;font-size:10px;padding:5px 0;font-weight:bold;}}@media print{{body{{background:white;padding:0;}}.no-print{{display:none;}}}}</style></head><body><button class='no-print' onclick='window.print()' style='padding:10px;margin-bottom:20px;cursor:pointer;'>🖨️ Print IDs</button><div class='page'>"
    for s in students:
        class_str = s.get('current_class') if s.get('current_class') and s.get('current_class') != 'Unassigned' else 'N/A'
        html += f"<div class='id-card'><div class='header'>{inst['school_name']}</div><div class='photo-box'><img src='/api/photo/{s['student_id']}' style='width:100%;height:100%;object-fit:cover;'></div><div class='details'><strong>Name:</strong> {s['first_name']} {s['last_name']}<br><strong>ID:</strong> {inst['school_name'][:3].upper()}-{s['student_id']:04d}<br><strong>Class/Prog:</strong> {class_str}<br><strong>Status:</strong> {s['boarding_status']}</div><div class='footer'>CONTACT: {inst['phone']} | {inst['address']}</div></div>"
    html += "</div></body></html>"
    return html

# --- ACADEMICS & GRADING ENGINE ---
@app.route('/api/grades', methods=['POST'])
@login_required
def add_grade():
    d = request.get_json()
    try:
        c_val = str(d.get('class_score') or '0').strip()
        e_val = str(d.get('exam_score') or '0').strip()
        c_score = int(c_val) if c_val else 0
        e_score = int(e_val) if e_val else 0
        stu_id = int(str(d.get('student_id') or '0').strip())
        if stu_id == 0: raise ValueError
    except ValueError: 
        return jsonify({"error": "Scores and Student ID must be valid numbers."}), 400
    
    t_score = c_score + e_score
    waec = get_waec_grade(t_score)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT student_id FROM students WHERE student_id = %s AND school_id = %s", (stu_id, current_user.school_id))
        if not cur.fetchone():
            return jsonify({"error": f"Student ID {stu_id} does not exist! Check the Digital Directory."}), 404
            
        subject_name = str(d.get('subject_name') or 'General').strip()[:100]
        cur.execute("SELECT subject_id FROM subjects WHERE subject_name = %s AND school_id = %s", (subject_name, current_user.school_id))
        sub = cur.fetchone()
        if not sub:
            cur.execute("INSERT INTO subjects (school_id, subject_name) VALUES (%s, %s) RETURNING subject_id", (current_user.school_id, subject_name))
            sub_id = cur.fetchone()['subject_id']
        else: 
            sub_id = sub['subject_id']
        
        academic_year = str(d.get('academic_year') or '').strip()[:9]
        term = str(d.get('term') or '').strip()[:20]
        remarks = str(d.get('remarks') or '').strip()[:250]

        cur.execute("INSERT INTO grades (school_id, student_id, subject_id, class_score, exam_score, total_score, waec_grade, academic_year, term, teacher_remarks) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", 
                    (current_user.school_id, stu_id, sub_id, c_score, e_score, t_score, waec, academic_year, term, remarks))
        conn.commit()
        return jsonify({"message": f"SBA Recorded! Total: {t_score}% ({waec})"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Database Error: {str(e)}"}), 400
    finally:
        cur.close()
        conn.close()

@app.route('/print_report/<int:student_id>', methods=['GET'])
@login_required
def print_report(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return "Access Denied.", 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
    student = cur.fetchone()
    if not student: return "Student record not found or access denied.", 404
    
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone()
    primary_color = inst['primary_color'] or '#0f4c81'
    
    cur.execute("SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.teacher_remarks, g.term, g.academic_year FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s AND g.school_id = %s ORDER BY g.academic_year DESC, g.term DESC, sub.subject_name", (student_id, current_user.school_id))
    grades = cur.fetchall(); cur.close(); conn.close()

    table_rows = ""
    for g in grades:
        table_rows += f"<tr><td>{g['subject_name']}</td><td>{g['class_score']}</td><td>{g['exam_score']}</td><td><strong>{g['total_score']}</strong></td><td><strong>{g['waec_grade']}</strong></td><td>{g['teacher_remarks']}</td><td>{g['term']} ({g['academic_year']})</td></tr>"

    if not table_rows: table_rows = "<tr><td colspan='7' style='text-align:center;'>No academic records found for this student.</td></tr>"

    html = f"""
    <!DOCTYPE html><html><head><title>Terminal Report | {student['first_name']} {student['last_name']}</title>
    <style>
        :root {{ --primary: {primary_color}; }}
        body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #eee; padding: 20px; color: #333; }}
        .page {{ background: white; max-width: 900px; margin: auto; padding: 40px; box-shadow: 0 0 15px rgba(0,0,0,0.1); border-radius: 8px; }}
        .header {{ display: flex; justify-content: space-between; border-bottom: 3px solid var(--primary); padding-bottom: 20px; margin-bottom: 30px; }}
        .school-name {{ color: var(--primary); font-size: 28px; font-weight: bold; margin: 0 0 5px 0; text-transform: uppercase; }}
        .student-details {{ font-size: 16px; line-height: 1.8; margin-top: 15px; }}
        .photo {{ width: 120px; height: 140px; border: 2px solid var(--primary); object-fit: cover; border-radius: 5px; }}
        table {{ width: 100%; border-collapse: collapse; margin-bottom: 40px; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; font-size: 14px; }}
        th {{ background: var(--primary); color: white; text-transform: uppercase; font-size: 13px; }}
        tr:nth-child(even) {{ background-color: #f9f9f9; }}
        .signatures {{ display: flex; justify-content: space-between; margin-top: 80px; padding: 0 20px; }}
        .sig-line {{ border-top: 2px solid #333; width: 250px; text-align: center; padding-top: 10px; font-weight: bold; font-size: 14px; text-transform: uppercase; }}
        .btn-print {{ padding:12px 25px; margin-bottom:20px; cursor:pointer; background:#28a745; color:white; border:none; border-radius:8px; font-weight:bold; font-size: 16px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        @media print {{ body {{ background: white; padding: 0; }} .page {{ box-shadow: none; max-width: 100%; padding: 0; }} .no-print {{ display: none; }} }}
    </style>
    </head><body>
    <div style="text-align: center;"><button class="no-print btn-print" onclick="window.print()">🖨️ Print Official Report</button></div>
    <div class="page">
        <div class="header">
            <div style="display:flex; gap: 20px;">
                <img src="/api/logo/{current_user.school_id}" style="width:90px; height:90px; object-fit:contain;">
                <div>
                    <h1 class="school-name">{inst['school_name']}</h1>
                    <div style="font-size:12px; color:#666; margin-bottom:15px;">{inst['address']} | Tel: {inst['phone']}</div>
                    <h2 style="margin: 0 0 10px 0; color: #555;">OFFICIAL TERMINAL REPORT</h2>
                </div>
            </div>
        </div>
        <div style="display:flex; justify-content: space-between; align-items: flex-end; margin-bottom: 25px;">
            <div class="student-details">
                <strong>STUDENT NAME:</strong> {student['first_name'].upper()} {student['last_name'].upper()}<br>
                <strong>STUDENT ID:</strong> {inst['school_name'][:3].upper()}-{student['student_id']:04d}<br>
                <strong>CURRENT CLASS:</strong> {student['current_class'].upper()}<br>
                <strong>BOARDING STATUS:</strong> {student['boarding_status'].upper()}
            </div>
            <img src="/api/photo/{student['student_id']}" class="photo">
        </div>
        <table>
            <tr><th>Subject</th><th>Class (30%)</th><th>Exam (70%)</th><th>Total (100%)</th><th>Grade</th><th>Teacher's Remarks</th><th>Academic Term</th></tr>
            {table_rows}
        </table>
        <div class="signatures">
            <div class="sig-line">Class Teacher's Signature</div>
            <div class="sig-line">Headmaster's Signature</div>
        </div>
    </div>
    </body></html>
    """
    return html

@app.route('/api/report_card/<int:student_id>', methods=['GET'])
@login_required
def get_report_card(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return jsonify({"error": "Access Denied."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.teacher_remarks, g.term FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s AND g.school_id = %s", (student_id, current_user.school_id))
    grades = cur.fetchall(); cur.close(); conn.close(); return jsonify({"grades": grades})

# --- FINANCIALS & BULK BILLING ---
@app.route('/api/fees/bulk_bill', methods=['POST'])
@login_required
def bulk_bill():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    d = request.get_json()
    target_class = str(d.get('target_class') or '').strip()
    amount_due = float(d.get('amount_due') or 0.0)
    academic_year = str(d.get('academic_year') or '').strip()[:9]
    term = str(d.get('term') or '').strip()[:20]
    desc = str(d.get('description') or '').strip()[:250]
    fee_cat = str(d.get('fee_category') or 'General')[:50]
    
    if not target_class or amount_due <= 0: return jsonify({"error": "Class and Amount Due are required."}), 400

    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO fees (school_id, student_id, fee_category, description, amount_due, academic_year, term)
            SELECT school_id, student_id, %s, %s, %s, %s, %s FROM students
            WHERE current_class = %s AND school_id = %s
            RETURNING fee_id
        """, (fee_cat, desc, amount_due, academic_year, term, target_class, current_user.school_id))
        billed = cur.fetchall()
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Executed Bulk Billing', f"Issued to {len(billed)} students in {target_class}"))
        conn.commit()
        return jsonify({"message": f"Bulk Bill issued successfully to {len(billed)} students in {target_class}!"}), 201
    except Exception as e:
        conn.rollback(); return jsonify({"error": f"Database Error: {str(e)}"}), 400
    finally: cur.close(); conn.close()

@app.route('/api/fees/bill', methods=['POST'])
@login_required
def bill_student():
    d = request.get_json()
    conn = get_db_connection(); cur = conn.cursor()
    try:
        student_id = int(d.get('student_id') or 0)
        amount_due = float(d.get('amount_due') or 0.0)

        cur.execute("SELECT student_id FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": f"Student ID {student_id} does not exist! Please check the Digital Directory."}), 404

        academic_year = str(d.get('academic_year') or '')[:9]
        term = str(d.get('term') or '')[:20]
        desc = str(d.get('description') or '')[:250]

        cur.execute("INSERT INTO fees (school_id, student_id, fee_category, description, amount_due, academic_year, term) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING fee_id", 
                    (current_user.school_id, student_id, d.get('fee_category'), desc, amount_due, academic_year, term))
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Issued Bill', f"GHS {amount_due} to ID {student_id}"))
        conn.commit(); return jsonify({"message": "Bill issued successfully!"}), 201
    except ValueError: return jsonify({"error": "Student ID and Amount Due must be numbers!"}), 400
    except Exception as e: conn.rollback(); return jsonify({"error": f"Database Error: {str(e)}"}), 400
    finally: cur.close(); conn.close()

@app.route('/api/fees/<int:fee_id>', methods=['DELETE'])
@login_required
def reverse_bill(fee_id):
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("DELETE FROM fees WHERE fee_id = %s AND school_id = %s RETURNING student_id, amount_due", (fee_id, current_user.school_id))
        result = cur.fetchone()
        if not result: return jsonify({"error": "Bill not found."}), 404
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Reversed Bill', f"Fee ID {fee_id} (GHS {result['amount_due']})"))
        conn.commit()
        return jsonify({"message": f"Bill {fee_id} successfully reversed."}), 200
    except Exception as e:
        conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/fees/pay', methods=['POST'])
@login_required
def log_payment():
    d = request.get_json()
    conn = get_db_connection(); cur = conn.cursor()
    try:
        fee_id = int(d.get('fee_id') or 0)
        amount_paid = float(d.get('amount_paid') or 0.0)

        cur.execute("SELECT fee_id FROM fees WHERE fee_id = %s AND school_id = %s", (fee_id, current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": f"Fee ID {fee_id} does not exist! Check the Statement."}), 404

        cur.execute("INSERT INTO payments (fee_id, amount_paid, payment_method) VALUES (%s, %s, %s)", 
                    (fee_id, amount_paid, d.get('payment_method')))
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Logged Payment', f"GHS {amount_paid} via {d.get('payment_method')} (Fee {fee_id})"))
        conn.commit(); return jsonify({"message": "Payment logged securely!"}), 201
    except ValueError: return jsonify({"error": "Fee ID and Amount Paid must be numbers!"}), 400
    except Exception as e: conn.rollback(); return jsonify({"error": f"Database Error: {str(e)}"}), 400
    finally: cur.close(); conn.close()

@app.route('/api/debtors', methods=['GET'])
@login_required
def get_debtors():
    conn = get_db_connection(); cur = conn.cursor()
    query = """
        SELECT s.student_id, s.first_name, s.last_name, s.guardian_contact,
               (SUM(f.amount_due) - COALESCE((SELECT SUM(amount_paid) FROM payments p JOIN fees f2 ON p.fee_id = f2.fee_id WHERE f2.student_id = s.student_id), 0)) as arrears
        FROM students s
        JOIN fees f ON s.student_id = f.student_id
        WHERE s.school_id = %s
        GROUP BY s.student_id, s.first_name, s.last_name, s.guardian_contact
        HAVING (SUM(f.amount_due) - COALESCE((SELECT SUM(amount_paid) FROM payments p JOIN fees f2 ON p.fee_id = f2.fee_id WHERE f2.student_id = s.student_id), 0)) > 0
        ORDER BY arrears DESC
    """
    cur.execute(query, (current_user.school_id,))
    debtors = cur.fetchall(); cur.close(); conn.close()
    return jsonify({"data": debtors})

@app.route('/api/statement/<int:student_id>', methods=['GET'])
@login_required
def get_statement(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return jsonify({"error": "Access Denied."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    query = "SELECT f.fee_id, f.student_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due, COALESCE(SUM(p.amount_paid), 0) as total_paid, (f.amount_due - COALESCE(SUM(p.amount_paid), 0)) as remaining_balance FROM fees f LEFT JOIN payments p ON f.fee_id = p.fee_id WHERE f.student_id = %s AND f.school_id = %s GROUP BY f.fee_id, f.student_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due ORDER BY f.date_issued DESC"
    cur.execute(query, (student_id, current_user.school_id))
    statement = cur.fetchall(); cur.close(); conn.close(); return jsonify({"statement": statement}), 200

@app.route('/api/analytics', methods=['GET'])
@login_required
def get_analytics():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(amount_due), 0) as total_due FROM fees WHERE school_id = %s", (current_user.school_id,))
    total_due = float(cur.fetchone()['total_due'])
    cur.execute("SELECT COALESCE(SUM(p.amount_paid), 0) as total_paid FROM payments p JOIN fees f ON p.fee_id = f.fee_id WHERE f.school_id = %s", (current_user.school_id,))
    total_paid = float(cur.fetchone()['total_paid'])
    cur.execute("SELECT COALESCE(SUM(amount), 0) as total_expenses FROM expenses WHERE school_id = %s", (current_user.school_id,))
    total_exp = float(cur.fetchone()['total_expenses'])
    cur.execute("SELECT waec_grade, COUNT(*) as count FROM grades WHERE school_id = %s GROUP BY waec_grade ORDER BY waec_grade", (current_user.school_id,))
    performance = cur.fetchall(); cur.close(); conn.close()
    return jsonify({"financials": {"due": total_due, "paid": total_paid, "outstanding": total_due - total_paid, "expenses": total_exp, "net_margin": total_paid - total_exp}, "performance": performance})

@app.route('/api/expenses', methods=['POST', 'GET'])
@login_required
def manage_expenses():
    conn = get_db_connection(); cur = conn.cursor()
    if request.method == 'POST':
        d = request.get_json()
        cur.execute("INSERT INTO expenses (school_id, category, description, amount) VALUES (%s, %s, %s, %s)", (current_user.school_id, d.get('category'), d.get('description'), d.get('amount')))
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Logged Expense', f"GHS {d.get('amount')} for {d.get('category')}"))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Expense logged securely."}), 201
    else:
        cur.execute("SELECT expense_id, category, description, amount, TO_CHAR(date_incurred, 'YYYY-MM-DD') as date FROM expenses WHERE school_id = %s ORDER BY date_incurred DESC", (current_user.school_id,))
        e = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": e})

@app.route('/api/attendance', methods=['POST'])
@login_required
def log_attendance():
    d = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO attendance (school_id, student_id, record_date, status) VALUES (%s, %s, %s, %s) ON CONFLICT (student_id, record_date) DO UPDATE SET status = EXCLUDED.status", (current_user.school_id, d.get('student_id'), d.get('record_date'), d.get('status')))
        conn.commit(); return jsonify({"message": f"Attendance recorded for {d.get('record_date')}"})
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

# --- LIVE SMS GATEWAY INTEGRATION ---
@app.route('/api/sms/blast', methods=['POST'])
@login_required
def send_sms_blast():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    data = request.get_json()
    audience = data.get('audience')
    message = data.get('message')

    if not message: return jsonify({"error": "Message body cannot be empty."}), 400

    conn = get_db_connection(); cur = conn.cursor()

    if audience == 'all':
        cur.execute("SELECT DISTINCT guardian_contact FROM students WHERE school_id = %s AND guardian_contact IS NOT NULL", (current_user.school_id,))
    elif audience == 'arrears':
        cur.execute("""
            SELECT DISTINCT s.guardian_contact
            FROM students s
            JOIN fees f ON s.student_id = f.student_id
            LEFT JOIN payments p ON f.fee_id = p.fee_id
            WHERE s.school_id = %s AND s.guardian_contact IS NOT NULL
            GROUP BY s.guardian_contact, f.fee_id, f.amount_due
            HAVING (f.amount_due - COALESCE(SUM(p.amount_paid), 0)) > 0
        """, (current_user.school_id,))
    elif audience == 'boarding':
        cur.execute("SELECT DISTINCT guardian_contact FROM students WHERE school_id = %s AND boarding_status = 'Boarding' AND guardian_contact IS NOT NULL", (current_user.school_id,))
    
    raw_contacts = cur.fetchall(); cur.close(); conn.close()
    contacts = [row['guardian_contact'].strip() for row in raw_contacts if row['guardian_contact']]

    if not contacts: return jsonify({"error": "No valid phone numbers found for this audience."}), 404

    SMS_API_KEY = os.environ.get('SMS_API_KEY')
    SMS_SENDER_ID = os.environ.get('SMS_SENDER_ID', 'SMS_ADMIN')

    if SMS_API_KEY:
        try:
            url = "https://sms.arkesel.com/api/v2/sms/send"
            headers = {"api-key": SMS_API_KEY}
            payload = {"sender": SMS_SENDER_ID, "message": message, "recipients": contacts}
            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code in [200, 201]:
                return jsonify({"message": f"Live SMS Blast sent successfully to {len(contacts)} parents!"}), 200
            else:
                return jsonify({"error": "Telecom Gateway rejected the request. Verify your API Key."}), 500
        except Exception as e: return jsonify({"error": f"Connection to Telecom Server failed: {str(e)}"}), 500
    else:
        return jsonify({"message": f"[SIMULATION] SMS processed for {len(contacts)} parents. Add SMS_API_KEY to Render to go live."}), 200

# --- THE HR VAULT & GUARDIAN ACCESS REGISTRATION ---
@app.route('/api/register_staff', methods=['POST'])
@login_required
def register_staff():
    if current_user.role != 'admin': return jsonify({"error": "Admin clearance required."}), 403
    d = request.get_json()
    hashed = generate_password_hash(d.get('password'))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        base_salary = float(d.get('salary') or 0.0)
        cur.execute("INSERT INTO system_users (school_id, email, password_hash, role, phone, subject, base_salary) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                    (current_user.school_id, d.get('email'), hashed, 'teacher', d.get('phone'), d.get('subject'), base_salary))
        conn.commit(); return jsonify({"message": "Staff Profile Created in HR Vault!"}), 201
    except Exception:
        conn.rollback(); return jsonify({"error": "Email exists or invalid data format."}), 409
    finally: cur.close(); conn.close()

@app.route('/api/register_guardian', methods=['POST'])
@login_required
def register_guardian():
    if current_user.role != 'admin': return jsonify({"error": "Admin clearance required."}), 403
    d = request.get_json()
    hashed = generate_password_hash(d.get('password'))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        stu_id = int(str(d.get('linked_student_id') or '0').strip())
        cur.execute("SELECT student_id FROM students WHERE student_id = %s AND school_id = %s", (stu_id, current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": "Student ID does not exist!"}), 404
        
        cur.execute("INSERT INTO system_users (school_id, email, password_hash, role, linked_student_id) VALUES (%s, %s, %s, %s, %s)", 
                    (current_user.school_id, d.get('email'), hashed, 'guardian', stu_id))
        conn.commit(); return jsonify({"message": f"Guardian Access created for Student ID {stu_id}!"}), 201
    except ValueError: return jsonify({"error": "Invalid Student ID."}), 400
    except Exception: conn.rollback(); return jsonify({"error": "Email already registered."}), 409
    finally: cur.close(); conn.close()

@app.route('/api/staff', methods=['GET'])
@login_required
def get_staff():
    if current_user.role != 'admin': return jsonify({"error": "Admin clearance required."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT email, phone, subject, base_salary FROM system_users WHERE school_id = %s AND role = 'teacher' ORDER BY email", (current_user.school_id,))
    staff = cur.fetchall(); cur.close(); conn.close()
    return jsonify({"data": staff})

# --- 8. THE FRONTEND DASHBOARD WITH SECURE VAULTS ---
@app.route('/dashboard')
def dashboard():
    school_name = "Global ERP Engine"
    primary_color = "#0f4c81"
    
    if current_user.is_authenticated and current_user.school_id:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT school_name, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
        inst = cur.fetchone(); cur.close(); conn.close()
        if inst:
            school_name = inst['school_name']
            primary_color = inst['primary_color'] or "#0f4c81"

    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{{ school_name }} | ERP Portal</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root { --primary: {{ primary_color }}; --secondary: #f4f7fa; --accent: #28a745; --text: #333; --danger: #dc3545; --info: #17a2b8; --warning: #ffc107;}
            body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--secondary); margin: 0; display: flex; color: var(--text); }
            
            .sidebar { width: 260px; background: var(--primary); color: white; min-height: 100vh; padding: 20px; box-sizing: border-box; position: fixed; overflow-y: auto; box-shadow: 2px 0 15px rgba(0,0,0,0.1); z-index: 100;}
            .sidebar-header { margin-bottom: 30px; padding-bottom: 15px; border-bottom: 1px solid rgba(255,255,255,0.15); display: flex; align-items: center; gap: 12px; }
            .sidebar h2 { margin: 0; font-size: 1.1rem; line-height: 1.3; font-weight: 600; letter-spacing: 0.5px;}
            .sidebar button { background: transparent; color: rgba(255,255,255,0.85); border: none; padding: 12px 15px; width: 100%; text-align: left; margin-bottom: 8px; border-radius: 8px; cursor: pointer; transition: all 0.2s ease; font-size: 0.95rem; font-weight: 500;}
            .sidebar button:hover { background: rgba(255,255,255,0.15); color: white; padding-left: 20px;}
            
            .main-content { margin-left: 260px; flex: 1; padding: 40px; box-sizing: border-box; min-height: 100vh; }
            
            /* Sleek Modern Cards */
            .card { background: white; padding: 30px; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.04); border: 1px solid #eaeaea; margin-bottom: 25px; transition: transform 0.2s ease;}
            h3 { margin-top: 0; color: var(--primary); border-bottom: 2px solid #f0f0f0; padding-bottom: 10px; margin-bottom: 20px;}
            
            /* Modern Inputs */
            input, select, textarea { width: 100%; padding: 12px; margin-bottom: 15px; border: 1px solid #ced4da; border-radius: 8px; box-sizing: border-box; transition: all 0.2s ease; font-family: inherit;}
            input:focus, select:focus, textarea:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(0,0,0,0.05); }
            
            /* Animated Buttons */
            .btn { background: var(--primary); color: white; border: none; padding: 12px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; width: 100%; margin-bottom: 10px; transition: all 0.3s ease; box-shadow: 0 4px 6px rgba(0,0,0,0.1);}
            .btn:hover { transform: translateY(-2px); box-shadow: 0 6px 12px rgba(0,0,0,0.15); filter: brightness(1.05);}
            .btn-success { background: var(--accent); } .btn-danger { background: var(--danger); } .btn-info { background: var(--info); } .btn-warning { background: var(--warning); color: #333;}
            
            .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            .metric-box { padding: 20px; border-radius: 12px; color: white; text-align: center; font-size: 1.1rem; font-weight: bold; box-shadow: 0 4px 15px rgba(0,0,0,0.1);}
            
            #toast { display: none; position: fixed; bottom: 30px; right: 30px; padding: 15px 25px; color: white; background: var(--accent); border-radius: 8px; z-index: 1000; font-weight: bold; box-shadow: 0 10px 30px rgba(0,0,0,0.2); animation: fadein 0.5s;}
            .hidden { display: none !important; }
            
            .table-row { transition: background 0.2s ease; }
            .table-row:hover { background-color: #f8f9fa !important; }
            
            @keyframes fadein { from {bottom: 0; opacity: 0;} to {bottom: 30px; opacity: 1;} }
        </style>
    </head>
    <body>
        <div id="toast">Message</div>
        <div class="sidebar">
            <div class="sidebar-header">
                {% if current_user.is_authenticated and current_user.school_id %}
                    <img src="/api/logo/{{ current_user.school_id }}" style="width: 45px; height: 45px; object-fit: contain; background: white; border-radius: 8px; padding: 2px;">
                {% else %}
                    <span style="font-size: 30px;">🌍</span>
                {% endif %}
                <h2>{{ school_name }}</h2>
            </div>
            
            {% if current_user.is_authenticated %}
                <div style="font-size: 0.8rem; color: rgba(255,255,255,0.6); margin-bottom: 20px; text-transform: uppercase; font-weight: bold; letter-spacing: 1px;">Access Level: {{ current_user.role }}</div>
                
                {% if current_user.role == 'superadmin' %}
                    <button onclick="window.location.reload()">🏢 Global Tenants</button>
                    <button onclick="showSection('sa-settings-section')">⚙️ Master Settings</button>
                    <button class="btn-success" onclick="sendAction('/api/setup_db', {}, true)" style="color:white; margin-top:20px;">🔄 Sync Database Engine</button>
                
                {% elif current_user.role == 'admin' %}
                    <button onclick="showSection('analytics-section')">📊 Corporate Dashboard</button>
                    <!-- SECURE SECTIONS WITH VAULT LOCK -->
                    <button onclick="secureSection('finance-section')">💰 Financials & Billing 🔒</button>
                    
                    <button onclick="showSection('admissions-section')">🎓 Admissions & Directory</button>
                    <button onclick="showSection('attendance-section')">📅 Roll Call & Feeding</button>
                    <button onclick="showSection('academics-section')">📚 Academic Reporting</button>
                    <button onclick="showSection('sms-section')">📟 Live SMS Gateway</button>
                    <button onclick="showSection('hr-section')">🧑‍🏫 Staff HR & Parent Access</button>
                    
                    <!-- SECURE SETTINGS -->
                    <button onclick="secureSection('settings-section')">⚙️ Security & Settings 🔒</button>
                
                {% elif current_user.role == 'teacher' %}
                    <button onclick="showSection('attendance-section')">📅 Daily Roll Call</button>
                    <button onclick="showSection('academics-section')">📚 SBA Grading Matrix</button>
                    <button onclick="showSection('settings-section')">⚙️ Account Security</button>
                
                {% elif current_user.role == 'guardian' %}
                    <button onclick="showSection('guardian-section')">👨‍👩‍👧 Guardian Portal</button>
                    <button onclick="showSection('settings-section')">⚙️ Account Security</button>
                {% endif %}
                
                <div style="margin-top: 40px; padding-bottom: 20px;">
                    <button class="btn-danger" style="color:white; box-shadow: 0 4px 15px rgba(220,53,69,0.3);" onclick="logout()">🛑 Secure Logout</button>
                </div>
            {% else %}
                <button class="btn-success" style="color:white;" onclick="sendAction('/api/setup_db', {}, true)">1. Sync System Core</button>
            {% endif %}
        </div>

        <main class="main-content">
            {% if current_user.is_authenticated and current_user.school_id %}
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 30px;">
                <div>
                    <h1 style="margin:0; color: var(--primary); font-size: 28px;">Enterprise Control Panel</h1>
                    <p style="margin: 5px 0 0 0; color: #666;">Welcome back to the {{ school_name }} administration portal.</p>
                </div>
            </div>
            {% else %}
                <h1 style="color: var(--primary); margin-bottom: 30px;">System Gateway</h1>
            {% endif %}

            {% if not current_user.is_authenticated %}
            <div class="card" style="max-width: 400px; margin: 50px auto; border-top: 5px solid var(--primary);" id="login-box">
                <h3 style="text-align: center; border:none;">Authorized Personnel Only</h3>
                <div id="login-fields">
                    <input type="email" id="email" placeholder="Official Email Address">
                    <input type="password" id="pass" placeholder="Secure Password">
                    <button class="btn" style="margin-top: 10px;" onclick="login()">Authenticate Login</button>
                </div>
            </div>
            
            {% elif current_user.role == 'guardian' %}
            <div id="guardian-section" class="card admin-section">
                <h3>👨‍👩‍👧 Guardian Access Portal</h3>
                <div class="grid-2">
                    <div style="background: #e9ecef; padding: 25px; border-radius: 12px; text-align: center; box-shadow: inset 0 2px 5px rgba(0,0,0,0.02);">
                        <h4 style="margin-top:0; color: var(--primary);">Academic Performance</h4>
                        <p style="font-size: 0.9rem; color: #555; margin-bottom: 20px;">View and download your ward's official end-of-term grading report directly to your device.</p>
                        <button class="btn btn-success" style="width:auto;" onclick="window.open('/print_report/{{ current_user.linked_student_id }}', '_blank')">🖨️ View & Print Terminal Report</button>
                    </div>
                    <div style="background: #fff3cd; padding: 25px; border-radius: 12px; text-align: center; border: 1px solid var(--warning);">
                        <h4 style="margin-top:0; color: #856404;">Financial Statement</h4>
                        <p style="font-size: 0.9rem; color: #555; margin-bottom: 20px;">Review issued fee bills, recorded payments, and check any outstanding tuition arrears.</p>
                        <button class="btn btn-warning" style="width:auto; color: #333;" onclick="loadStatement({{ current_user.linked_student_id }})">View Financial Ledger</button>
                    </div>
                </div>
            </div>

            {% elif current_user.role == 'superadmin' %}
            
            <!-- Super Admin New Settings Section -->
            <div id="sa-settings-section" class="card admin-section hidden" style="border: 2px solid #333;">
                <h3>⚙️ Master Settings & Credentials</h3>
                <p style="font-size:0.9rem; color:#555;">Update the root Super Admin email and password. If you change your email, use the new one on your next login.</p>
                <div class="grid-2">
                    <input type="email" id="saNewEmail" placeholder="New Super Admin Email (Optional)">
                    <input type="password" id="saNewPass" placeholder="New Secure Password (Optional)">
                </div>
                <button class="btn" style="background:#333;" onclick="updateSACredentials()">Update Master Credentials</button>
            </div>

            <div class="card" style="border: 2px solid var(--accent);">
                <h3>🚀 Provision New School Tenant</h3>
                <div class="grid-2">
                    <div><input type="text" id="onboardSchool" placeholder="Official School Name"><input type="email" id="onboardEmail" placeholder="Administrator Email"></div>
                    <div><input type="password" id="onboardPass" placeholder="Temporary Password"><button class="btn btn-success" onclick="onboardNewSchool()">Provision SaaS Tenant</button></div>
                </div>
            </div>
            
            <div id="branding-div" class="card hidden" style="border: 2px solid var(--warning); background: #fffdf5;">
                <h3>🎨 White-Label Branding Engine</h3>
                <p style="font-size:0.9rem; color:#555;">Customize the theme color and official crest for <strong id="brandSchoolName"></strong>.</p>
                <input type="hidden" id="brandSchoolId">
                <div class="grid-2">
                    <div>
                        <label style="font-weight:bold; display:block; margin-bottom:8px;">Primary Theme Color</label>
                        <input type="color" id="brandColor" style="height: 50px; padding: 5px; cursor:pointer;">
                    </div>
                    <div>
                        <label style="font-weight:bold; display:block; margin-bottom:8px;">School Crest / Logo Upload</label>
                        <input type="file" id="brandLogo" accept="image/png, image/jpeg" style="background:white;">
                    </div>
                </div>
                <div style="display:flex; gap: 10px; margin-top: 10px;">
                    <button class="btn btn-warning" style="color:#333;" onclick="saveBranding()">Apply Theme & Refresh</button>
                    <button class="btn" style="background:#666;" onclick="document.getElementById('branding-div').classList.add('hidden')">Cancel</button>
                </div>
            </div>

            <div class="card" style="border: 2px solid var(--danger);">
                <h3>🔑 Super Admin Master Override</h3>
                <p style="font-size:0.9rem; color:#555;">Force reset a locked-out School Admin's password globally across all databases.</p>
                <div class="grid-2">
                    <input type="email" id="saResetEmail" placeholder="Target Admin Email Address">
                    <input type="password" id="saResetPass" placeholder="Assign New Temporary Password">
                </div>
                <button class="btn btn-danger" onclick="saResetAdmin()">Execute Global Override</button>
            </div>

            <div class="card">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 20px;">
                    <h3 style="margin:0; border:none;">Global Server Tenants</h3>
                    <button class="btn" style="width:auto; margin:0;" onclick="loadSchools()">🔄 Refresh Server Data</button>
                </div>
                <div id="school-container"></div>
            </div>

            {% elif current_user.role in ['admin', 'teacher'] %}
            
                {% if current_user.role == 'admin' %}
                <!-- Analytics Section -->
                <div id="analytics-section" class="card admin-section">
                    <h3>Corporate Dashboard Overview</h3>
                    <div class="grid-2" style="margin-bottom: 30px;">
                        <div class="metric-box" style="background: var(--primary);" id="metricRevenue">Gross Revenue: GHS 0.00</div>
                        <div class="metric-box" style="background: var(--danger);" id="metricExpenses">Total Expenses: GHS 0.00</div>
                        <div class="metric-box" style="background: var(--accent); grid-column: span 2;" id="metricMargin">Net Operational Margin: GHS 0.00</div>
                    </div>
                    <div class="grid-2">
                        <div style="position: relative; height: 300px; padding:15px; border:1px solid #eee; border-radius:12px;"><canvas id="financeChart"></canvas></div>
                        <div style="position: relative; height: 300px; padding:15px; border:1px solid #eee; border-radius:12px;"><canvas id="waecChart"></canvas></div>
                    </div>
                </div>

                <!-- Financials Section (Vault Locked) -->
                <div id="finance-section" class="card admin-section hidden" style="border-top: 5px solid var(--danger);">
                    <h3>💰 Financial Ledger & Billing Operations (SECURED)</h3>
                    <div class="grid-2">
                        <div>
                            <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #eee;">
                                <h4 style="margin-top:0; color: var(--primary);">1. Issue Segmented Bill (Individual)</h4>
                                <input type="number" id="bStuId" placeholder="Student ID (Required)">
                                <select id="bCat">
                                    <option value="Consolidated Fee">Consolidated Term Fee</option>
                                    <option value="Tuition">Tuition Fee</option>
                                    <option value="PTA Dues">PTA Dues</option>
                                    <option value="Feeding Fee">Feeding Fee</option>
                                    <option value="Exams Fee">Exams Fee</option>
                                    <option value="Arrears (Past Term)">Arrears (Past Term)</option>
                                </select>
                                <div class="grid-2">
                                    <input type="text" id="bTerm" placeholder="Term (e.g. Term 1)">
                                    <input type="text" id="bYear" placeholder="Year (e.g. 2026)">
                                </div>
                                <input type="number" id="bAmount" placeholder="Amount Due (GHS)">
                                <input type="text" id="bDesc" placeholder="Description / Memo">
                                <button class="btn btn-success" onclick="issueBill()">Issue Bill & View Ledger</button>
                            </div>

                            <div style="background: #e2e3e5; padding: 20px; border-radius: 12px; border: 1px solid #ccc;">
                                <h4 style="margin-top:0; color:#333;">⚡ Automated Bulk Class Billing</h4>
                                <p style="font-size: 0.85rem; color: #555;">Instantly issue the exact same fee to every active student in a specific class cohort.</p>
                                <input type="text" id="bbClass" placeholder="Target Class (e.g., Basic 3)">
                                <select id="bbCat"><option value="Consolidated Fee">Consolidated Term Fee</option><option value="Tuition">Tuition Fee</option><option value="PTA Dues">PTA Dues</option></select>
                                <div class="grid-2">
                                    <input type="text" id="bbTerm" placeholder="Term">
                                    <input type="text" id="bbYear" placeholder="Year">
                                </div>
                                <input type="number" id="bbAmount" placeholder="Amount Due (GHS)">
                                <input type="text" id="bbDesc" placeholder="Memo / Description">
                                <button class="btn btn-primary" onclick="bulkBillClass()">Execute Bulk Billing Protocol</button>
                            </div>
                        </div>

                        <div>
                            <div style="background: #e9ecef; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #dee2e6;">
                                <h4 style="margin-top:0; color: var(--primary);">2. Student Ledger & Payments</h4>
                                <p style="font-size: 0.85rem; color: #555;">Search a student to view active bills, check real-time balances, record cash payments, or reverse billing errors.</p>
                                <div class="grid-2" style="align-items: center;">
                                    <input type="number" id="stateStuId" placeholder="Target Student ID" style="margin-bottom:0;">
                                    <button class="btn" style="margin-bottom:0;" onclick="loadStatement()">Open Secure Ledger</button>
                                </div>
                            </div>
                            
                            <div style="background: #fff3cd; padding: 20px; border-radius: 12px; border: 1px solid var(--warning); margin-bottom: 20px;">
                                <h4 style="margin-top:0; color: #856404;">3. Live Arrears & Debtors Radar</h4>
                                <p style="font-size: 0.85rem; color: #666;">Generate an instant report of all active students with negative ledger balances.</p>
                                <button class="btn btn-warning" style="color:#333;" onclick="loadDebtors()">Scan Database for Debtors</button>
                            </div>
                            
                            <div style="background: #f8d7da; padding: 20px; border-radius: 12px; border: 1px solid var(--danger);">
                                <h4 style="margin-top:0; color: #721c24;">4. Log Operational Cash Outflow</h4>
                                <select id="eCat"><option value="Staff Salaries">Staff Salaries</option><option value="Boarding Provisions">Boarding Provisions</option><option value="Utilities">Facility Utilities</option><option value="Maintenance">Maintenance & Repairs</option></select>
                                <div class="grid-2"><input type="text" id="eDesc" placeholder="Memo"><input type="number" id="eAmount" placeholder="Amount (GHS)"></div>
                                <button class="btn btn-danger" onclick="sendAction('/api/expenses', {category: document.getElementById('eCat').value, description: document.getElementById('eDesc').value, amount: document.getElementById('eAmount').value})">Log Cash Outflow</button>
                                <button class="btn btn-info" style="margin-bottom:0;" onclick="loadExpenses()">Review Expense Ledger</button>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Admissions Section -->
                <div id="admissions-section" class="card grid-2 admin-section hidden" style="border-top: 5px solid var(--primary);">
                    <div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #eee;">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px;">Enroll New Student (Manual)</h3>
                            <div class="grid-2"><input type="text" id="sFirst" placeholder="First Name"><input type="text" id="sLast" placeholder="Last Name"></div>
                            <div class="grid-2">
                                <input type="text" id="sClass" placeholder="Class / Program (e.g., Basic 1)">
                                <select id="sBoarding"><option value="Day">Day Student</option><option value="Boarding">Boarding Student</option></select>
                            </div>
                            <div class="grid-2">
                                <input type="text" id="sHouse" placeholder="House (or N/A)">
                                <input type="text" id="sGName" placeholder="Guardian Name">
                            </div>
                            <div class="grid-2" style="margin-bottom: 15px;">
                                <input type="text" id="sGContact" placeholder="Guardian Contact">
                                <div>
                                    <label style="font-size:0.8rem; font-weight:bold; display:block; margin-bottom: 5px; color:#555;">Passport Photo (Auto-Compress)</label>
                                    <input type="file" id="sPhoto" accept="image/*" style="margin-bottom: 0;">
                                </div>
                            </div>
                            <button class="btn btn-success" onclick="enrollStudent()">Register Student into Database</button>
                        </div>
                        
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #dee2e6;">
                            <h4 style="margin-top:0; color: var(--primary);">Bulk CSV Enrollment Engine</h4>
                            <p style="font-size: 0.85rem; color: #555;">Upload a standard CSV file. Columns MUST match exactly: <br><i>First Name, Last Name, Class, Guardian Name, Guardian Contact, Boarding Status, House</i>.</p>
                            <div style="display:flex; gap:10px; align-items:center;">
                                <input type="file" id="csvUpload" accept=".csv" style="margin-bottom:0; background:white;">
                                <button class="btn btn-primary" style="margin-bottom:0; width:60%;" onclick="bulkEnrollCSV()">Import Roster</button>
                            </div>
                        </div>
                    </div>
                    <div>
                        <div style="background: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #eee; box-shadow: 0 4px 6px rgba(0,0,0,0.02);">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px;">ID & Directory Tools</h3>
                            <div class="grid-2">
                                <button class="btn btn-warning" style="color:#333;" onclick="window.open('/print_ids', '_blank')">🖨️ Generate Batch ID Cards</button>
                                <button class="btn btn-info" onclick="loadRoster()">View Digital Directory</button>
                            </div>
                        </div>
                        
                        <div style="background: #fff3cd; padding: 20px; border-radius: 12px; border: 1px solid var(--warning); margin-bottom: 20px;">
                            <h4 style="margin-top:0; color: #856404;">✏️ Data Correction Editor</h4>
                            <p style="font-size: 0.85rem; color: #666;">Fix typos or update a student's profile without deleting their financial history.</p>
                            <input type="number" id="editStuId" placeholder="Target Student ID">
                            <div class="grid-2">
                                <input type="text" id="editFirst" placeholder="Corrected First Name">
                                <input type="text" id="editLast" placeholder="Corrected Last Name">
                            </div>
                            <div class="grid-2">
                                <input type="text" id="editClass" placeholder="Corrected Class">
                                <select id="editBoarding"><option value="Day">Day Student</option><option value="Boarding">Boarding Student</option></select>
                            </div>
                            <button class="btn btn-warning" onclick="editStudent()" style="color:#333;">Save Corrections to Record</button>
                        </div>

                        <div style="background: #e2e3e5; padding: 20px; border-radius: 12px; border: 1px solid #ccc;">
                            <h4 style="margin-top:0; color: #333;">End-of-Year Promotion Engine</h4>
                            <p style="font-size:0.85rem; color:#555;">Move an entire class cohort up to the next academic grade level instantly.</p>
                            <div class="grid-2">
                                <input type="text" id="promoFrom" placeholder="Current Class">
                                <input type="text" id="promoTo" placeholder="Next Class">
                            </div>
                            <button class="btn btn-primary" onclick="promoteClass()">Promote Entire Cohort</button>
                        </div>
                    </div>
                </div>
                {% endif %}

                <!-- Shared Teacher/Admin Sections -->
                <div id="attendance-section" class="card admin-section {% if current_user.role == 'admin' %}hidden{% endif %}" style="border-top: 5px solid var(--info);">
                    <h3>📅 Daily Roll Call & Feeding Optimization Tracker</h3>
                    <div style="background: #e9ecef; padding: 25px; border-radius: 12px; max-width: 800px; border: 1px solid #dee2e6;">
                        <div class="grid-2" style="align-items: end;">
                            <div>
                                <label style="font-weight:bold; font-size:0.9rem; color:#555; display:block; margin-bottom:5px;">Select Date</label>
                                <input type="date" id="attDate" value="" style="margin-bottom:0;">
                            </div>
                            <div>
                                <label style="font-weight:bold; font-size:0.9rem; color:#555; display:block; margin-bottom:5px;">Target Student ID</label>
                                <input type="number" id="attStuId" placeholder="ID" style="margin-bottom:0;">
                            </div>
                        </div>
                        <div class="grid-2" style="margin-top: 20px;">
                            <select id="attStatus" style="margin-bottom:0; font-weight:bold;">
                                <option value="Present">✅ Present (Include in Daily Feeding)</option>
                                <option value="Absent">❌ Absent (Remove from Daily Feeding)</option>
                            </select>
                            <button class="btn btn-info" style="margin-bottom:0;" onclick="sendAction('/api/attendance', {student_id: document.getElementById('attStuId').value, record_date: document.getElementById('attDate').value, status: document.getElementById('attStatus').value})">Commit Attendance Record</button>
                        </div>
                    </div>
                </div>

                <div id="academics-section" class="card grid-2 admin-section hidden" style="border-top: 5px solid var(--accent);">
                    <div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; border: 1px solid #eee;">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px; color:var(--primary);">Record SBA Grade (30/70 Matrix)</h3>
                            <div class="grid-2">
                                <input type="number" id="gStuId" placeholder="Student ID (Required)">
                                <input type="text" id="gSub" placeholder="Subject Name">
                            </div>
                            <div class="grid-2">
                                <input type="number" id="gClass" placeholder="Class Score (30%)">
                                <input type="number" id="gExam" placeholder="Exam Score (70%)">
                            </div>
                            <div class="grid-2">
                                <input type="text" id="gTerm" placeholder="Term (e.g. Term 1)">
                                <input type="text" id="gYear" placeholder="Year (e.g. 2026)">
                            </div>
                            <input type="text" id="gRem" placeholder="Teacher's Qualitative Remark (e.g. Very impressive)">
                            <button class="btn btn-success" onclick="sendAction('/api/grades', {student_id: document.getElementById('gStuId').value, subject_name: document.getElementById('gSub').value, class_score: document.getElementById('gClass').value, exam_score: document.getElementById('gExam').value, term: document.getElementById('gTerm').value, academic_year: document.getElementById('gYear').value, remarks: document.getElementById('gRem').value})">Save Academic Record to Vault</button>
                        </div>
                    </div>
                    <div>
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #dee2e6; height: 100%; box-sizing: border-box;">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px; color:var(--primary);">Terminal Report Generation</h3>
                            <p style="font-size: 0.9rem; color: #555; line-height: 1.5; margin-bottom: 20px;">Review raw grading inputs in the table viewer to check for data entry errors, or generate a beautifully formatted, official print-ready report card for the student.</p>
                            <label style="font-weight:bold; font-size:0.9rem; color:#555; display:block; margin-bottom:5px;">Target Student ID</label>
                            <input type="number" id="repId" placeholder="Enter ID to pull records...">
                            <div class="grid-2" style="margin-top: 15px;">
                                <button class="btn btn-info" onclick="loadReport()">View Raw Data</button>
                                <button class="btn btn-success" onclick="printReportCard()">🖨️ Print Official Report</button>
                            </div>
                        </div>
                    </div>
                </div>

                {% if current_user.role == 'admin' %}
                <!-- SMS Desk Section -->
                <div id="sms-section" class="card admin-section hidden" style="border-top: 5px solid var(--info);">
                    <h3>📟 Live SMS Communication Desk</h3>
                    <div class="grid-2">
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; border: 1px solid #eee;">
                            <label style="font-weight:bold; display:block; margin-bottom:10px; font-size:1.1rem; color:var(--primary);">1. Select Target Audience</label>
                            <select id="smsAudience" style="font-size:1.05rem; padding:15px;">
                                <option value="all">Broadcast to ALL Active Parents</option>
                                <option value="arrears">Only Parents with Unpaid Tuition Arrears</option>
                                <option value="boarding">Only Parents of Boarding Students</option>
                            </select>
                            <p style="font-size:0.85rem; color:#666; line-height:1.5; margin-top:15px;">The engine will automatically scan the database, isolate the relevant guardian phone numbers, filter out duplicates, and route the payload to the national telecom gateway.</p>
                        </div>
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #dee2e6;">
                            <label style="font-weight:bold; display:block; margin-bottom:10px; font-size:1.1rem; color:var(--primary);">2. Compose Payload</label>
                            <textarea id="smsBody" placeholder="Type your official broadcast message here..." style="height: 120px; resize: none;"></textarea>
                            <button class="btn btn-info" style="font-size:1.1rem; padding:15px;" onclick="sendAction('/api/sms/blast', {audience: document.getElementById('smsAudience').value, message: document.getElementById('smsBody').value})">Deploy SMS Broadcast 🚀</button>
                        </div>
                    </div>
                </div>

                <!-- Expanded Staff HR Vault & Guardian Access -->
                <div id="hr-section" class="card grid-2 admin-section hidden" style="border-top: 5px solid #6c757d;">
                    <div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #eee;">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px; color:var(--primary);">Register Staff Profile</h3>
                            <div class="grid-2">
                                <input type="email" id="tEmail" placeholder="Teacher Email (Login ID)">
                                <input type="password" id="tPass" placeholder="Temporary Password">
                            </div>
                            <div class="grid-2">
                                <input type="text" id="tPhone" placeholder="Mobile Number">
                                <input type="text" id="tSubj" placeholder="Primary Assigned Subject">
                            </div>
                            <input type="number" id="tSal" placeholder="Monthly Base Salary (GHS)">
                            <button class="btn btn-success" onclick="registerStaff()">Add Teacher to HR Directory</button>
                        </div>
                        
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #dee2e6;">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:10px; color:var(--primary);">Guardian Portal Access</h3>
                            <p style="font-size:0.85rem; color:#555; margin-bottom:15px;">Create a secure login for a parent. They will only be able to view their specific ward's terminal reports and financial ledgers.</p>
                            <div class="grid-2">
                                <input type="email" id="gEmail" placeholder="Parent Email Address">
                                <input type="password" id="gPass" placeholder="Secure Password">
                            </div>
                            <input type="number" id="gStuId" placeholder="Target Linked Student ID">
                            <button class="btn btn-primary" onclick="registerGuardian()">Grant Parent Portal Access</button>
                        </div>
                    </div>
                    <div>
                        <div style="background: white; padding: 20px; border-radius: 12px; border: 1px solid #eee; height: 100%; box-sizing: border-box; box-shadow: 0 4px 6px rgba(0,0,0,0.02);">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px; color:var(--primary);">Compliance & Payroll Vault</h3>
                            <p style="font-size: 0.95rem; color: #555; line-height:1.6; margin-bottom:25px;">Maintain strictly confidential digital records of your academic teaching staff to ensure instant data readiness for Ghana Education Service (GES) auditing and internal monthly payroll calculations.</p>
                            <button class="btn btn-info" onclick="loadStaff()">View Official Staff Directory</button>
                        </div>
                    </div>
                </div>
                {% endif %}
            {% endif %}

            <!-- UNIVERSAL SETTINGS & SECURITY TAB (Visible to ALL logged-in users, Vault Locked) -->
            {% if current_user.is_authenticated and current_user.role != 'superadmin' %}
            <div id="settings-section" class="card admin-section hidden" style="border-top: 5px solid #333;">
                <h3>🔒 Security & Access Management (SECURED)</h3>
                
                <div class="grid-2">
                    <div style="background: #fff; padding: 25px; border-radius: 12px; border: 1px solid #ccc; box-shadow: 0 4px 10px rgba(0,0,0,0.05);">
                        <h4 style="margin-top:0; color: #333;">Change Personal Password</h4>
                        <p style="font-size: 0.9rem; color: #555; margin-bottom: 20px;">Change the temporary password you were given to a secure, private password.</p>
                        <input type="password" id="myOldPass" placeholder="Current Password">
                        <input type="password" id="myNewPass" placeholder="New Secure Password">
                        <button class="btn" style="background:#333;" onclick="changeMyPassword()">Update My Password</button>
                    </div>

                    {% if current_user.role == 'admin' %}
                    <div style="background: #f8d7da; padding: 25px; border-radius: 12px; border: 1px solid var(--danger);">
                        <h4 style="margin-top:0; color: #721c24;">Master Credential Override</h4>
                        <p style="font-size: 0.9rem; color: #721c24; margin-bottom: 20px;">Force-reset a forgotten password for any Teacher or Guardian, or instantly lock out an old staff member.</p>
                        <input type="email" id="resetTargetEmail" placeholder="Target User Email (e.g. teacher@school.com)">
                        <input type="password" id="resetNewPass" placeholder="Assign New Temporary Password">
                        <button class="btn btn-danger" onclick="adminResetPassword()">Execute Force Reset</button>
                    </div>
                    {% endif %}
                </div>

                {% if current_user.role == 'admin' %}
                <div style="background: #fff; padding: 25px; border-radius: 12px; border: 1px solid #ccc; box-shadow: 0 4px 10px rgba(0,0,0,0.05); margin-top: 20px;">
                    <h4 style="margin-top:0; color: #333;">📜 Security Audit Trail</h4>
                    <p style="font-size: 0.9rem; color: #555; margin-bottom: 20px;">Review an immutable ledger of every sensitive action (deletions, payments, profile edits) taken by staff members. Protects against internal fraud.</p>
                    <button class="btn btn-primary" onclick="loadAuditLogs()">View Master Audit Logs</button>
                </div>

                <h3 style="margin-top:40px;">⚙️ Institution Profile Settings</h3>
                <div style="background: #f8f9fa; padding: 25px; border-radius: 12px; border: 1px solid #eee; max-width: 600px;">
                    <p style="font-size: 0.9rem; color: #555; margin-bottom: 20px;">Update your school's official contact details. These will print directly onto the headers of your generated Student ID Cards and Terminal Report Cards.</p>
                    <label style="font-weight:bold; color:var(--primary); margin-bottom:8px; display:block;">Official School Address / Location</label>
                    <input type="text" id="setAddress" placeholder="e.g., P.O Box 123, Winneba, Central Region" style="font-size:1.05rem;">
                    
                    <label style="font-weight:bold; color:var(--primary); margin-bottom:8px; display:block; margin-top:15px;">Official Contact Number</label>
                    <input type="text" id="setPhone" placeholder="e.g., 0244123456" style="font-size:1.05rem;">
                    
                    <button class="btn btn-success" style="margin-top: 15px; font-size:1.05rem;" onclick="sendAction('/api/settings', {address: document.getElementById('setAddress').value, phone: document.getElementById('setPhone').value})">Save Profile Updates</button>
                </div>
                <p style="font-size: 0.85rem; color: #888; margin-top: 20px;"><i>Note: To change your institution's name, core theme color, or logo, please contact your Super Admin.</i></p>
                {% endif %}
            </div>
            {% endif %}

            <!-- Shared Data Viewer (With Offline CSV Export) -->
            {% if current_user.is_authenticated %}
            <div class="card hidden" id="data-viewer" style="border: 2px solid var(--primary); box-shadow: 0 15px 35px rgba(0,0,0,0.1); border-radius: 16px; overflow:hidden; padding: 0;">
                <div style="background: var(--primary); padding: 20px 30px; display:flex; justify-content:space-between; align-items:center;">
                    <h3 id="viewer-title" style="border:none; margin:0; padding:0; color:white; font-size:1.2rem;">Data Explorer</h3>
                    <button class="btn" style="background:rgba(255,255,255,0.2); color:white; width:auto; margin:0; border-radius:6px; box-shadow:none;" onclick="exportTableToCSV('Exported_Data.csv')">⬇️ Download to Excel/CSV</button>
                </div>
                <div id="table-container" style="padding: 30px; overflow-x: auto;"></div>
            </div>
            {% endif %}
        </main>

        <script>
            let unlockedSections = {};
            let inactivityTimeout;

            // Security Auto-Logout Timer (15 mins)
            function resetTimer() {
                clearTimeout(inactivityTimeout);
                if(document.getElementById('email') == null) {
                    inactivityTimeout = setTimeout(() => {
                        alert("Security Alert: Your session has expired due to 15 minutes of inactivity.");
                        logout();
                    }, 15 * 60 * 1000);
                }
            }
            window.onload = () => { loadDashboardData(); resetTimer(); };
            document.onmousemove = resetTimer;
            document.onkeypress = resetTimer;

            function showSection(sectionId) {
                const sections = ['analytics-section', 'finance-section', 'admissions-section', 'attendance-section', 'academics-section', 'sms-section', 'hr-section', 'guardian-section', 'settings-section', 'sa-settings-section'];
                sections.forEach(id => {
                    const el = document.getElementById(id);
                    if(el) el.classList.add('hidden');
                });
                const target = document.getElementById(sectionId);
                if(target) target.classList.remove('hidden');
                const viewer = document.getElementById('data-viewer');
                if(viewer) viewer.classList.add('hidden');
                if (sectionId === 'analytics-section') loadDashboardData();
            }

            // --- THE VAULT LOCK ENGINE ---
            async function secureSection(sectionId) {
                if (unlockedSections[sectionId]) {
                    showSection(sectionId);
                    return;
                }
                const pass = prompt("SECURE VAULT: Please enter your personal password to access this restricted section.");
                if (!pass) return;
                
                try {
                    const res = await fetch('/api/verify_password', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({password: pass})
                    });
                    if(res.ok) {
                        unlockedSections[sectionId] = true;
                        showSection(sectionId);
                        showToast("Vault Unlocked Successfully");
                    } else {
                        const data = await res.json();
                        showToast(data.error, true);
                    }
                } catch(e) { showToast("Connection failed", true); }
            }

            function showToast(message, isError=false) {
                const toast = document.getElementById('toast');
                toast.innerText = message;
                toast.style.background = isError ? '#dc3545' : '#28a745';
                toast.style.display = 'block';
                setTimeout(() => { toast.style.display = 'none'; }, 5000);
            }

            // CSV EXPORT ENGINE
            function downloadCSV(csv, filename) {
                let csvFile = new Blob([csv], {type: "text/csv"});
                let downloadLink = document.createElement("a");
                downloadLink.download = filename;
                downloadLink.href = window.URL.createObjectURL(csvFile);
                downloadLink.style.display = "none";
                document.body.appendChild(downloadLink);
                downloadLink.click();
            }

            function exportTableToCSV(filename) {
                let csv = [];
                let rows = document.querySelectorAll("#table-container table tr");
                for (let i = 0; i < rows.length; i++) {
                    let row = [], cols = rows[i].querySelectorAll("td, th");
                    for (let j = 0; j < cols.length; j++) {
                        let text = cols[j].innerText.replace(/"/g, '""');
                        if (text.includes("🗑️") || text.includes("💰") || text.includes("Open Ledger") || text.includes("Pay Bill") || text.includes("Reverse Bill")) continue; 
                        row.push('"' + text + '"');
                    }
                    if(row.length > 0) csv.push(row.join(","));
                }
                downloadCSV(csv.join(String.fromCharCode(10)), filename);
            }

            async function login() {
                const emailInput = document.getElementById('email').value;
                const res = await fetch('/api/login', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email: emailInput, password: document.getElementById('pass').value})
                });
                const data = await res.json();
                if (res.ok) { 
                    showToast(data.message); 
                    setTimeout(() => window.location.reload(), 1000); 
                } else { showToast(data.error, true); }
            }

            async function logout() { await fetch('/api/logout', { method: 'POST' }); window.location.reload(); }

            async function sendAction(endpoint, payload, isGet=false) {
                try {
                    const options = isGet ? {} : { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) };
                    const res = await fetch(endpoint, options);
                    const data = await res.json();
                    if (res.ok) { 
                        showToast(data.message || "Success"); 
                        if (endpoint === '/api/setup_db') loadSchools(); 
                        if (endpoint === '/api/expenses') loadDashboardData();
                    }
                    else if (res.status === 402) showToast(data.error, true); 
                    else showToast(data.error || "Error", true);
                } catch(e) { showToast("Connection failed", true); }
            }

            // --- CREDENTIAL MANAGEMENT ---
            async function updateSACredentials() {
                const email = document.getElementById('saNewEmail').value;
                const pass = document.getElementById('saNewPass').value;
                if(!email && !pass) { showToast("Enter a new email or password.", true); return; }
                if(!confirm("Are you sure you want to update the master credentials?")) return;
                
                try {
                    const res = await fetch('/api/superadmin/credentials', { 
                        method: 'POST', headers: {'Content-Type': 'application/json'}, 
                        body: JSON.stringify({new_email: email, new_password: pass}) 
                    });
                    const data = await res.json();
                    if(res.ok) { 
                        showToast(data.message); 
                        document.getElementById('saNewEmail').value = ''; document.getElementById('saNewPass').value = ''; 
                    } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function changeMyPassword() {
                const oldP = document.getElementById('myOldPass').value;
                const newP = document.getElementById('myNewPass').value;
                if(!oldP || !newP) { showToast("Provide both passwords.", true); return; }
                try {
                    const res = await fetch('/api/change_password', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({current_password: oldP, new_password: newP}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('myOldPass').value = ''; document.getElementById('myNewPass').value = ''; } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function adminResetPassword() {
                const email = document.getElementById('resetTargetEmail').value;
                const newP = document.getElementById('resetNewPass').value;
                if(!email || !newP) { showToast("Provide Target Email and New Password.", true); return; }
                if(!confirm(`Are you sure you want to FORCE RESET the password for ${email}?`)) return;
                try {
                    const res = await fetch('/api/admin/reset_password', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({target_email: email, new_password: newP}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('resetTargetEmail').value = ''; document.getElementById('resetNewPass').value = ''; } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function saResetAdmin() {
                const email = document.getElementById('saResetEmail').value;
                const newP = document.getElementById('saResetPass').value;
                if(!email || !newP) { showToast("Provide Target Email and New Password.", true); return; }
                if(!confirm(`Are you sure you want to FORCE RESET the admin password for ${email}?`)) return;
                try {
                    const res = await fetch('/api/superadmin/reset_admin', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({admin_email: email, new_password: newP}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('saResetEmail').value = ''; document.getElementById('saResetPass').value = ''; } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }
            
            async function enrollStudent() {
                const fileInput = document.getElementById('sPhoto');
                let photo_b64 = null;
                if (fileInput.files.length > 0) {
                    const file = fileInput.files[0];
                    photo_b64 = await new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onload = function(e) {
                            const img = new Image();
                            img.onload = function() {
                                const canvas = document.createElement('canvas');
                                const MAX_WIDTH = 300; 
                                const scaleSize = MAX_WIDTH / img.width;
                                canvas.width = MAX_WIDTH;
                                canvas.height = img.height * scaleSize;
                                const ctx = canvas.getContext('2d');
                                ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                                resolve(canvas.toDataURL('image/jpeg', 0.7)); 
                            };
                            img.src = e.target.result;
                        };
                        reader.readAsDataURL(file);
                    });
                }
                const payload = {
                    first_name: document.getElementById('sFirst').value,
                    last_name: document.getElementById('sLast').value,
                    current_class: document.getElementById('sClass').value,
                    guardian_name: document.getElementById('sGName').value,
                    guardian_contact: document.getElementById('sGContact').value,
                    boarding_status: document.getElementById('sBoarding').value,
                    house: document.getElementById('sHouse').value,
                    photo_b64: photo_b64
                };
                sendAction('/api/students', payload);
            }

            async function editStudent() {
                const id = document.getElementById('editStuId').value;
                if(!id) { showToast("Enter Target Student ID", true); return; }
                const payload = {
                    first_name: document.getElementById('editFirst').value,
                    last_name: document.getElementById('editLast').value,
                    current_class: document.getElementById('editClass').value,
                    boarding_status: document.getElementById('editBoarding').value,
                    guardian_contact: prompt("Update Guardian Phone Number:"),
                    house: prompt("Update House Assignment:")
                };
                try {
                    const res = await fetch('/api/students/' + id, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); loadRoster(); } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function bulkEnrollCSV() {
                const fileInput = document.getElementById('csvUpload');
                if (!fileInput.files.length) { showToast("Please select a CSV file.", true); return; }
                const formData = new FormData();
                formData.append('file', fileInput.files[0]);
                showToast("Uploading roster, please wait...", false);
                try {
                    const res = await fetch('/api/students/bulk', { method: 'POST', body: formData });
                    const data = await res.json();
                    if (res.ok) { showToast(data.message); loadRoster(); }
                    else showToast(data.error, true);
                } catch(e) { showToast("Upload failed", true); }
                fileInput.value = ''; 
            }

            async function bulkBillClass() {
                const payload = {
                    target_class: document.getElementById('bbClass').value,
                    fee_category: document.getElementById('bbCat').value,
                    amount_due: document.getElementById('bbAmount').value,
                    academic_year: document.getElementById('bbYear').value,
                    term: document.getElementById('bbTerm').value,
                    description: document.getElementById('bbDesc').value
                };
                try {
                    const res = await fetch('/api/fees/bulk_bill', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('bbAmount').value = ''; document.getElementById('bbDesc').value = ''; } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function deleteStudent(id) {
                if(!confirm("WARNING: This will permanently delete the student and ALL associated grades, fees, and attendance records. Continue?")) return;
                try {
                    const res = await fetch('/api/students/' + id, { method: 'DELETE' });
                    const data = await res.json();
                    if (res.ok) { showToast(data.message); loadRoster(); }
                    else showToast(data.error, true);
                } catch(e) { showToast("Connection failed", true); }
            }

            async function deleteBill(feeId, stuId) {
                if(!confirm("Are you sure you want to completely REVERSE this bill and delete its record?")) return;
                try {
                    const res = await fetch('/api/fees/' + feeId, { method: 'DELETE' });
                    const data = await res.json();
                    if (res.ok) { showToast(data.message); loadStatement(stuId); }
                    else showToast(data.error, true);
                } catch(e) { showToast("Connection failed", true); }
            }

            async function promoteClass() {
                const fromC = document.getElementById('promoFrom').value;
                const toC = document.getElementById('promoTo').value;
                if(!fromC || !toC) { showToast("Enter both classes", true); return; }
                if(!confirm("Are you sure you want to promote ALL students currently in " + fromC + " to " + toC + "?")) return;
                
                try {
                    const res = await fetch('/api/students/promote', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({from_class: fromC, to_class: toC}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); loadRoster(); }
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function registerStaff() {
                const payload = {
                    email: document.getElementById('tEmail').value,
                    password: document.getElementById('tPass').value,
                    phone: document.getElementById('tPhone').value,
                    subject: document.getElementById('tSubj').value,
                    salary: document.getElementById('tSal').value
                };
                try {
                    const res = await fetch('/api/register_staff', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) { 
                        showToast(data.message); 
                        document.getElementById('tEmail').value = ''; document.getElementById('tPass').value = ''; 
                        document.getElementById('tPhone').value = ''; document.getElementById('tSubj').value = ''; document.getElementById('tSal').value = '';
                        loadStaff(); 
                    } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function registerGuardian() {
                const payload = {
                    email: document.getElementById('gEmail').value,
                    password: document.getElementById('gPass').value,
                    linked_student_id: document.getElementById('gStuId').value
                };
                try {
                    const res = await fetch('/api/register_guardian', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) { 
                        showToast(data.message); 
                        document.getElementById('gEmail').value = ''; document.getElementById('gPass').value = ''; document.getElementById('gStuId').value = '';
                    } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function issueBill() {
                const stuId = document.getElementById('bStuId').value;
                const payload = {
                    student_id: stuId,
                    fee_category: document.getElementById('bCat').value,
                    amount_due: document.getElementById('bAmount').value,
                    academic_year: document.getElementById('bYear').value,
                    term: document.getElementById('bTerm').value,
                    description: document.getElementById('bDesc').value
                };
                try {
                    const res = await fetch('/api/fees/bill', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) {
                        showToast(data.message);
                        document.getElementById('bAmount').value = ''; document.getElementById('bDesc').value = '';
                        loadStatement(stuId);
                    } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function processPayment(feeId, balance, stuId) {
                if(balance <= 0) { showToast("This bill is fully paid.", true); return; }
                const amount = prompt("Enter payment amount (GHS) for Fee ID " + feeId + ". Outstanding Balance: GHS " + balance);
                if(!amount || isNaN(amount) || amount <= 0) return;
                const method = prompt("Enter payment method (Cash / MoMo):", "Cash");
                if(!method) return;
                
                try {
                    const res = await fetch('/api/fees/pay', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({fee_id: feeId, amount_paid: amount, payment_method: method}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); loadStatement(stuId); } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            function printReportCard() {
                const id = document.getElementById('repId').value;
                if (!id) { showToast("Please enter a Student ID", true); return; }
                window.open('/print_report/' + id, '_blank');
            }

            // --- SUPERADMIN SPECIFIC FUNCTIONS ---
            function openBrandingModal(schoolId, currentColor, schoolName) {
                document.getElementById('branding-div').classList.remove('hidden');
                document.getElementById('brandSchoolId').value = schoolId;
                document.getElementById('brandColor').value = currentColor || '#0f4c81';
                document.getElementById('brandSchoolName').innerText = schoolName;
                document.getElementById('branding-div').scrollIntoView({behavior: "smooth"});
            }

            async function saveBranding() {
                const id = document.getElementById('brandSchoolId').value;
                const color = document.getElementById('brandColor').value;
                const fileInput = document.getElementById('brandLogo');
                let logo_b64 = null;
                
                if (fileInput.files.length > 0) {
                    const file = fileInput.files[0];
                    logo_b64 = await new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onload = function(e) { resolve(e.target.result); };
                        reader.readAsDataURL(file);
                    });
                }
                try {
                    const res = await fetch('/api/superadmin/branding/' + id, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({color: color, logo_b64: logo_b64}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); loadSchools(); document.getElementById('branding-div').classList.add('hidden'); }
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function loadSchools() {
                const res = await fetch('/api/superadmin/schools');
                if(!res.ok) return;
                const data = await res.json();
                let html = '<table style="width:100%; border-collapse: collapse; text-align: left;"><tr><th style="padding:15px; border-bottom:2px solid #ddd;">ID</th><th style="padding:15px; border-bottom:2px solid #ddd;">School Name</th><th style="padding:15px; border-bottom:2px solid #ddd;">Expiry Date</th><th style="padding:15px; border-bottom:2px solid #ddd;">Status</th><th style="padding:15px; border-bottom:2px solid #ddd;">Actions</th></tr>';
                data.data.forEach(s => {
                    const statusColor = s.status === 'Active' ? 'green' : 'red';
                    html += `<tr>
                        <td style="padding:15px; border-bottom:1px solid #eee; font-weight:bold;">${s.school_id}</td>
                        <td style="padding:15px; border-bottom:1px solid #eee;">
                            <div style="display:flex; align-items:center; gap:10px;">
                                <div style="width:15px; height:15px; border-radius:50%; background:${s.primary_color};"></div>
                                <strong>${s.school_name}</strong>
                            </div>
                        </td>
                        <td style="padding:15px; border-bottom:1px solid #eee;">${s.expiry_date}</td>
                        <td style="color:${statusColor}; font-weight:bold; padding:15px; border-bottom:1px solid #eee;">${s.status}</td>
                        <td style="padding:15px; border-bottom:1px solid #eee;">
                            <button class="btn btn-warning" style="width: auto; padding: 6px 12px; margin: 2px; color:#333;" onclick="openBrandingModal(${s.school_id}, '${s.primary_color}', '${s.school_name}')">🎨 Brand</button>
                            <button class="btn btn-success" style="width: auto; padding: 6px 12px; margin: 2px;" onclick="sendAction('/api/superadmin/renew/${s.school_id}', {})">Renew</button>
                            <button class="btn btn-info" style="width: auto; padding: 6px 12px; margin: 2px;" onclick="window.location.href='/api/superadmin/backup/${s.school_id}'">⬇️ Backup</button>
                            <input type="file" id="file_${s.school_id}" accept=".json" style="display:none;" onchange="uploadRestore(${s.school_id})">
                            <button class="btn btn-danger" style="width: auto; padding: 6px 12px; margin: 2px;" onclick="document.getElementById('file_${s.school_id}').click()">⬆️ Restore</button>
                        </td>
                    </tr>`;
                });
                document.getElementById('school-container').innerHTML = html + '</table>';
            }

            async function onboardNewSchool() {
                const res = await fetch('/api/superadmin/onboard', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({school_name: document.getElementById('onboardSchool').value, admin_email: document.getElementById('onboardEmail').value, admin_password: document.getElementById('onboardPass').value})
                });
                const data = await res.json();
                if (res.ok) { showToast(data.message); loadSchools(); } else { showToast(data.error, true); }
            }

            async function uploadRestore(schoolId) {
                const fileInput = document.getElementById('file_' + schoolId);
                if (!fileInput.files.length) return;
                const formData = new FormData();
                formData.append('file', fileInput.files[0]);
                showToast("Restoring data, please wait...", false);
                try {
                    const res = await fetch('/api/superadmin/restore/' + schoolId, { method: 'POST', body: formData });
                    const data = await res.json();
                    if (res.ok) showToast(data.message);
                    else showToast(data.error, true);
                } catch(e) { showToast("Upload failed", true); }
                fileInput.value = ''; 
            }

            // --- DATA RENDERERS ---
            function renderTable(title, headers, rows, keys) {
                const viewer = document.getElementById('data-viewer');
                document.getElementById('viewer-title').innerText = title;
                const container = document.getElementById('table-container');
                if (!rows || rows.length === 0) { container.innerHTML = '<div style="padding: 20px; color:#666;">No records found in database.</div>'; viewer.classList.remove('hidden'); return; }
                let html = '<table style="width:100%; border-collapse: collapse;"><tr>';
                headers.forEach(h => html += `<th style="background:#f8f9fa; color:var(--primary); padding:15px; text-align:left; border-bottom: 2px solid #ddd; font-weight:bold;">${h}</th>`);
                html += '</tr>';
                rows.forEach(row => {
                    html += '<tr class="table-row">';
                    keys.forEach(k => {
                        let val = row[k];
                        if (k === 'photo') {
                            html += `<td style="padding:15px; border-bottom:1px solid #eee; width: 60px;"><img src="/api/photo/${row['student_id']}" style="width:45px; height:45px; border-radius:8px; object-fit:cover; border:2px solid #ccc; background:#eee;"></td>`;
                        } else if (k === 'action_pay') {
                            html += `<td style="padding:15px; border-bottom:1px solid #eee;">`;
                            if (row['remaining_balance'] > 0) {
                                html += `<button class="btn btn-success" style="padding:6px 12px; font-size:0.85rem; margin:0; width:auto;" onclick="processPayment(${row['fee_id']}, ${row['remaining_balance']}, ${row['student_id']})">Pay Bill</button>`;
                            } else {
                                html += `<span style="color:var(--accent); font-weight:bold; margin-right: 15px;">Cleared</span>`;
                            }
                            html += `<button class="btn btn-danger" style="padding:6px 12px; font-size:0.85rem; margin:0 0 0 5px; width:auto; background:white; color:var(--danger); border:1px solid var(--danger); box-shadow:none;" onclick="deleteBill(${row['fee_id']}, ${row['student_id']})">Reverse Bill</button></td>`;
                        } else if (k === 'action_roster') {
                            html += `<td style="padding:15px; border-bottom:1px solid #eee;">
                                <button class="btn btn-danger" style="padding:6px 12px; font-size:0.85rem; margin:0; width:auto;" onclick="deleteStudent(${row['student_id']})">🗑️ Delete</button>
                            </td>`;
                        } else if (k === 'action_debtor') {
                            html += `<td style="padding:15px; border-bottom:1px solid #eee;"><button class="btn btn-info" style="padding:6px 12px; font-size:0.85rem; margin:0; width:auto;" onclick="loadStatement(${row['student_id']})">Open Ledger</button></td>`;
                        } else if (k === 'remaining_balance' || k === 'arrears') {
                            let color = val > 0 ? '#dc3545' : '#28a745';
                            html += `<td style="color:${color}; font-weight:bold; padding:15px; border-bottom:1px solid #eee;">${val}</td>`;
                        } else if (k === 'boarding_status') {
                            let badge = val === 'Boarding' ? 'background:var(--primary);color:white;' : 'background:#e9ecef;color:#555;';
                            html += `<td style="padding:15px; border-bottom:1px solid #eee;"><span style="${badge}padding:4px 10px;border-radius:12px;font-size:0.8rem; font-weight:bold;">${val}</span></td>`;
                        } else {
                            html += `<td style="padding:15px; border-bottom:1px solid #eee;">${val}</td>`;
                        }
                    });
                    html += '</tr>';
                });
                container.innerHTML = html + '</table>';
                viewer.classList.remove('hidden'); 
                viewer.scrollIntoView({behavior: "smooth"});
            }

            async function loadRoster() {
                const res = await fetch('/api/students'); const data = await res.json();
                renderTable("Student Roster", ['Photo', 'ID', 'First Name', 'Last Name', 'Class', 'Status', 'House', 'Guardian Contact', 'Action'], data.data, ['photo', 'student_id', 'first_name', 'last_name', 'current_class', 'boarding_status', 'house', 'guardian_contact', 'action_roster']);
            }
            async function loadStaff() {
                const res = await fetch('/api/staff'); const data = await res.json();
                renderTable("Staff Directory", ['Teacher Email', 'Phone Number', 'Subject', 'Base Salary (GHS)'], data.data, ['email', 'phone', 'subject', 'base_salary']);
            }
            async function loadExpenses() {
                const res = await fetch('/api/expenses'); const data = await res.json();
                renderTable("Expense Ledger", ['Date', 'Category', 'Description', 'Amount (GHS)'], data.data, ['date', 'category', 'description', 'amount']);
            }
            async function loadDebtors() {
                const res = await fetch('/api/debtors'); const data = await res.json();
                renderTable("Arrears & Debtors Tracker", ['ID', 'First Name', 'Last Name', 'Parent Contact', 'Total Owed (GHS)', 'Action'], data.data, ['student_id', 'first_name', 'last_name', 'guardian_contact', 'arrears', 'action_debtor']);
            }
            async function loadAuditLogs() {
                const res = await fetch('/api/audit_logs'); const data = await res.json();
                renderTable("Master Security Audit Trail", ['Timestamp', 'System User (Email)', 'Executed Action', 'Target Record / Details'], data.data, ['time', 'user_email', 'action', 'target']);
            }
            async function loadReport() {
                const id = document.getElementById('repId').value;
                if(!id) { showToast("Please enter a Student ID", true); return; }
                const res = await fetch('/api/report_card/' + id);
                if (!res.ok) { showToast("Not Found", true); return; }
                const data = await res.json();
                renderTable("Raw Grading Data", ['Subject', 'Class', 'Exam', 'Total', 'Grade', 'Remarks', 'Term'], data.grades, ['subject_name', 'class_score', 'exam_score', 'total_score', 'waec_grade', 'teacher_remarks', 'term']);
            }
            async function loadStatement(overrideId) {
                const id = overrideId || (document.getElementById('stateStuId') ? document.getElementById('stateStuId').value : null);
                if(!id) { showToast("Please enter a Student ID", true); return; }
                const res = await fetch('/api/statement/' + id);
                if (!res.ok) { showToast("Ledger Not Found", true); return; }
                const data = await res.json();
                
                if(document.getElementById('stateStuId')) {
                    renderTable("Financial Ledger (Student ID: " + id + ")", ['Bill Type', 'Year', 'Term', 'Desc', 'Due', 'Paid', 'Balance', 'Action'], data.statement, ['fee_category', 'academic_year', 'term', 'description', 'amount_due', 'total_paid', 'remaining_balance', 'action_pay']);
                } else {
                    renderTable("Financial Ledger (Student ID: " + id + ")", ['Bill Type', 'Year', 'Term', 'Desc', 'Due', 'Paid', 'Balance'], data.statement, ['fee_category', 'academic_year', 'term', 'description', 'amount_due', 'total_paid', 'remaining_balance']);
                }
            }

            let financeChartInstance = null;
            let waecChartInstance = null;

            async function loadDashboardData() {
                if (document.getElementById('attDate')) {
                    document.getElementById('attDate').value = new Date().toISOString().split('T')[0];
                }

                if (document.getElementById('school-container')) {
                    loadSchools();
                }
                
                if (document.getElementById('financeChart')) {
                    try {
                        const res = await fetch('/api/analytics'); const data = await res.json();
                        document.getElementById('metricRevenue').innerText = `Gross Revenue: GHS ${data.financials.paid.toLocaleString()}`;
                        document.getElementById('metricExpenses').innerText = `Total Expenses: GHS ${data.financials.expenses.toLocaleString()}`;
                        document.getElementById('metricMargin').innerText = `Net Margin: GHS ${data.financials.net_margin.toLocaleString()}`;
                        
                        if (financeChartInstance) financeChartInstance.destroy();
                        financeChartInstance = new Chart(document.getElementById('financeChart').getContext('2d'), {
                            type: 'doughnut', data: { labels: ['Revenue', 'Arrears', 'Expenses'], datasets: [{ data: [data.financials.paid, data.financials.outstanding, data.financials.expenses], backgroundColor: ['#28a745', '#ffc107', '#dc3545'], borderWidth: 0 }] }, options: { responsive: true, maintainAspectRatio: false }
                        });

                        const labels = data.performance.map(p => p.waec_grade); const counts = data.performance.map(p => p.count);
                        if (waecChartInstance) waecChartInstance.destroy();
                        waecChartInstance = new Chart(document.getElementById('waecChart').getContext('2d'), {
                            type: 'bar', data: { labels: labels, datasets: [{ label: 'Students', data: counts, backgroundColor: '{{ primary_color }}' }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { title: { display: true, text: 'WAEC Grade Distribution' } } }
                        });
                    } catch(e) {}
                }
            }
            window.onload = loadDashboardData;
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, current_user=current_user, school_name=school_name, primary_color=primary_color)

# --- AUTOMATIC BOOT SEQUENCE ---
with app.app_context():
    try:
        initialize_database()
        print("SaaS Database Engine synchronized successfully on boot.")
    except Exception as e:
        print(f"Boot synchronization skipped (Database may be asleep): {e}")

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
