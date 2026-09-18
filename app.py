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

# --- SETUP & BACKUP ---
@app.route('/api/setup_db')
def setup_db():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS institutions (school_id SERIAL PRIMARY KEY, school_name VARCHAR(150) NOT NULL UNIQUE, subscription_expiry_date DATE NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS students (student_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, first_name VARCHAR(100) NOT NULL, last_name VARCHAR(100) NOT NULL, guardian_name VARCHAR(100) NOT NULL, guardian_contact VARCHAR(20) NOT NULL, enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS boarding_status VARCHAR(20) DEFAULT 'Day'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS house VARCHAR(100) DEFAULT 'Unassigned'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS photo_key VARCHAR(255)")
    cur.execute("CREATE TABLE IF NOT EXISTS system_users (user_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, email VARCHAR(100) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, linked_student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE)")
    cur.execute("CREATE TABLE IF NOT EXISTS subjects (subject_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100) NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS grades (grade_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, subject_id INTEGER REFERENCES subjects(subject_id) ON DELETE CASCADE, class_score INTEGER NOT NULL, exam_score INTEGER NOT NULL, total_score INTEGER NOT NULL, waec_grade VARCHAR(2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL, teacher_remarks VARCHAR(255))")
    cur.execute("CREATE TABLE IF NOT EXISTS fees (fee_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, fee_category VARCHAR(50) NOT NULL, description VARCHAR(255) NOT NULL, amount_due DECIMAL(10, 2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL, date_issued TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS payments (payment_id SERIAL PRIMARY KEY, fee_id INTEGER REFERENCES fees(fee_id) ON DELETE CASCADE, amount_paid DECIMAL(10, 2) NOT NULL, payment_method VARCHAR(50) NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS expenses (expense_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, category VARCHAR(50) NOT NULL, description VARCHAR(255) NOT NULL, amount DECIMAL(10, 2) NOT NULL, date_incurred TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS attendance (attendance_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, record_date DATE NOT NULL, status VARCHAR(20) NOT NULL, UNIQUE(student_id, record_date))")
    cur.execute("SELECT * FROM system_users WHERE role = 'superadmin'")
    if not cur.fetchone():
        hashed_sa = generate_password_hash('ceo123')
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s)", ('superadmin@engine.com', hashed_sa, 'superadmin'))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"message": "Multi-Tenant SaaS Engine Ready!"})

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

# --- AUTH & SUPER ADMIN ---
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

@app.route('/api/superadmin/schools', methods=['GET'])
@login_required
def get_schools():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT school_id, school_name, TO_CHAR(subscription_expiry_date, 'YYYY-MM-DD') as expiry_date, CASE WHEN subscription_expiry_date >= CURRENT_DATE THEN 'Active' ELSE 'Expired' END as status FROM institutions ORDER BY school_id")
    schools = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": schools})

@app.route('/api/superadmin/renew/<int:school_id>', methods=['POST'])
@login_required
def renew_school(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("UPDATE institutions SET subscription_expiry_date = CURRENT_DATE + INTERVAL '1 year' WHERE school_id = %s RETURNING school_name, subscription_expiry_date", (school_id,))
    updated = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    return jsonify({"message": f"Contract Renewed! {updated['school_name']} active until {updated['subscription_expiry_date']}"})

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
                cur.execute("INSERT INTO students (student_id, school_id, first_name, last_name, guardian_name, guardian_contact, boarding_status, house, photo_key, enrollment_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (student_id) DO NOTHING", (r['student_id'], school_id, r['first_name'], r['last_name'], r['guardian_name'], r['guardian_contact'], r.get('boarding_status', 'Day'), r.get('house', 'Unassigned'), r.get('photo_key'), r['enrollment_date']))
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

# --- TENANT ENDPOINTS ---
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
        cur.execute("INSERT INTO students (school_id, first_name, last_name, guardian_name, guardian_contact, boarding_status, house, photo_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING student_id", 
                    (current_user.school_id, data.get('first_name'), data.get('last_name'), data.get('guardian_name'), data.get('guardian_contact'), data.get('boarding_status', 'Day'), data.get('house', 'Unassigned'), photo_key))
        new_id = cur.fetchone()['student_id']
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": f"Student Enrolled successfully! ID: {new_id}"}), 201
    else:
        cur.execute("SELECT student_id, first_name, last_name, boarding_status, house, guardian_contact FROM students WHERE school_id = %s ORDER BY student_id DESC", (current_user.school_id,))
        students = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": students})

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

@app.route('/print_ids', methods=['GET'])
@login_required
def print_ids():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT student_id, first_name, last_name, boarding_status, house FROM students WHERE school_id = %s ORDER BY student_id", (current_user.school_id,))
    students = cur.fetchall()
    cur.execute("SELECT school_name FROM institutions WHERE school_id = %s", (current_user.school_id,))
    school_name = cur.fetchone()['school_name']; cur.close(); conn.close()
    html = f"<!DOCTYPE html><html><head><title>Print IDs</title><style>body{{font-family:Arial;background:#f0f0f0;padding:20px;}}.page{{display:grid;grid-template-columns:repeat(2,1fr);gap:15px;max-width:800px;margin:auto;}}.id-card{{background:white;border:2px solid #0f4c81;border-radius:8px;padding:15px;width:350px;height:200px;box-sizing:border-box;position:relative;overflow:hidden;}}.header{{background:#0f4c81;color:white;text-align:center;padding:5px;margin:-15px -15px 10px -15px;border-radius:6px 6px 0 0;font-weight:bold;font-size:14px;}}.photo-box{{width:70px;height:90px;border:1px solid #ccc;float:left;margin-right:15px;background:#eee;}}.details{{float:left;font-size:12px;line-height:1.6;width:calc(100% - 90px);}}.footer{{position:absolute;bottom:0;left:0;width:100%;background:#eee;text-align:center;font-size:10px;padding:5px 0;font-weight:bold;}}@media print{{body{{background:white;padding:0;}}.no-print{{display:none;}}}}</style></head><body><button class='no-print' onclick='window.print()' style='padding:10px;margin-bottom:20px;cursor:pointer;'>🖨️ Print IDs</button><div class='page'>"
    for s in students:
        html += f"<div class='id-card'><div class='header'>{school_name}</div><div class='photo-box'><img src='/api/photo/{s['student_id']}' style='width:100%;height:100%;object-fit:cover;'></div><div class='details'><strong>Name:</strong> {s['first_name']} {s['last_name']}<br><strong>ID:</strong> {school_name[:3].upper()}-{s['student_id']:04d}<br><strong>Status:</strong> {s['boarding_status']}<br><strong>House:</strong> {s['house']}</div><div class='footer'>VALID FOR CURRENT ACADEMIC YEAR ONLY</div></div>"
    html += "</div></body></html>"
    return html

@app.route('/api/analytics', methods=['GET'])
@login_required
def get_analytics():
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

# --- UPGRADED DATABASE VALIDATION ENGINE ---
@app.route('/api/fees/bill', methods=['POST'])
@login_required
def bill_student():
    d = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        student_id = int(d.get('student_id') or 0)
        amount_due = float(d.get('amount_due') or 0.0)

        # Pre-check: Does this student actually exist?
        cur.execute("SELECT student_id FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
        if not cur.fetchone():
            return jsonify({"error": f"Student ID {student_id} does not exist! Please check the Digital Directory."}), 404

        # Enforce strict string limits matching PostgreSQL constraints
        academic_year = str(d.get('academic_year') or '')[:9]
        term = str(d.get('term') or '')[:20]
        desc = str(d.get('description') or '')[:250]

        cur.execute("INSERT INTO fees (school_id, student_id, fee_category, description, amount_due, academic_year, term) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING fee_id", 
                    (current_user.school_id, student_id, d.get('fee_category'), desc, amount_due, academic_year, term))
        conn.commit()
        return jsonify({"message": "Bill issued successfully!"}), 201
    except ValueError:
        return jsonify({"error": "Student ID and Amount Due must be numbers!"}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Database Error: {str(e)}"}), 400
    finally:
        cur.close()
        conn.close()

@app.route('/api/fees/pay', methods=['POST'])
@login_required
def log_payment():
    d = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        fee_id = int(d.get('fee_id') or 0)
        amount_paid = float(d.get('amount_paid') or 0.0)

        cur.execute("SELECT fee_id FROM fees WHERE fee_id = %s AND school_id = %s", (fee_id, current_user.school_id))
        if not cur.fetchone():
            return jsonify({"error": f"Fee ID {fee_id} does not exist! Check the Statement."}), 404

        cur.execute("INSERT INTO payments (fee_id, amount_paid, payment_method) VALUES (%s, %s, %s)", 
                    (fee_id, amount_paid, d.get('payment_method')))
        conn.commit()
        return jsonify({"message": "Payment logged securely!"}), 201
    except ValueError:
        return jsonify({"error": "Fee ID and Amount Paid must be numbers!"}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Database Error: {str(e)}"}), 400
    finally:
        cur.close()
        conn.close()

@app.route('/api/grades', methods=['POST'])
@login_required
def add_grade():
    d = request.get_json()
    try:
        c_score = int(d.get('class_score') or 0)
        e_score = int(d.get('exam_score') or 0)
        stu_id = int(d.get('student_id') or 0)
    except ValueError: 
        return jsonify({"error": "Scores and ID must be numbers."}), 400
    
    t_score = c_score + e_score
    waec = get_waec_grade(t_score)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT student_id FROM students WHERE student_id = %s AND school_id = %s", (stu_id, current_user.school_id))
        if not cur.fetchone():
            return jsonify({"error": f"Student ID {stu_id} does not exist! Check the Digital Directory."}), 404
            
        cur.execute("SELECT subject_id FROM subjects WHERE subject_name = %s AND school_id = %s", (d.get('subject_name'), current_user.school_id))
        sub = cur.fetchone()
        if not sub:
            cur.execute("INSERT INTO subjects (school_id, subject_name) VALUES (%s, %s) RETURNING subject_id", (current_user.school_id, d.get('subject_name')))
            sub_id = cur.fetchone()['subject_id']
        else: 
            sub_id = sub['subject_id']
        
        academic_year = str(d.get('academic_year') or '')[:9]
        term = str(d.get('term') or '')[:20]
        remarks = str(d.get('remarks') or '')[:250]

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

@app.route('/api/report_card/<int:student_id>', methods=['GET'])
@login_required
def get_report_card(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return jsonify({"error": "Access Denied."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.teacher_remarks, g.term FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s AND g.school_id = %s", (student_id, current_user.school_id))
    grades = cur.fetchall(); cur.close(); conn.close(); return jsonify({"grades": grades})

@app.route('/api/statement/<int:student_id>', methods=['GET'])
@login_required
def get_statement(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return jsonify({"error": "Access Denied."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    query = "SELECT f.fee_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due, COALESCE(SUM(p.amount_paid), 0) as total_paid, (f.amount_due - COALESCE(SUM(p.amount_paid), 0)) as remaining_balance FROM fees f LEFT JOIN payments p ON f.fee_id = p.fee_id WHERE f.student_id = %s AND f.school_id = %s GROUP BY f.fee_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due ORDER BY f.date_issued DESC"
    cur.execute(query, (student_id, current_user.school_id))
    statement = cur.fetchall(); cur.close(); conn.close(); return jsonify({"statement": statement}), 200

# --- LIVE SMS GATEWAY INTEGRATION ---
@app.route('/api/sms/blast', methods=['POST'])
@login_required
def send_sms_blast():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    data = request.get_json()
    audience = data.get('audience')
    message = data.get('message')

    if not message:
        return jsonify({"error": "Message body cannot be empty."}), 400

    conn = get_db_connection()
    cur = conn.cursor()

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
    
    raw_contacts = cur.fetchall()
    cur.close(); conn.close()

    contacts = [row['guardian_contact'].strip() for row in raw_contacts if row['guardian_contact']]

    if not contacts:
        return jsonify({"error": "No valid phone numbers found for this audience."}), 404

    SMS_API_KEY = os.environ.get('SMS_API_KEY')
    SMS_SENDER_ID = os.environ.get('SMS_SENDER_ID', 'SMS_ADMIN')

    if SMS_API_KEY:
        try:
            url = "https://sms.arkesel.com/api/v2/sms/send"
            headers = {"api-key": SMS_API_KEY}
            payload = {
                "sender": SMS_SENDER_ID,
                "message": message,
                "recipients": contacts
            }
            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code in [200, 201]:
                return jsonify({"message": f"Live SMS Blast sent successfully to {len(contacts)} parents!"}), 200
            else:
                return jsonify({"error": "Telecom Gateway rejected the request. Verify your API Key."}), 500
        except Exception as e:
            return jsonify({"error": f"Connection to Telecom Server failed: {str(e)}"}), 500
    else:
        return jsonify({"message": f"[SIMULATION] SMS processed for {len(contacts)} parents. Add SMS_API_KEY to Render to go live."}), 200

@app.route('/api/register_staff', methods=['POST'])
@login_required
def register_staff():
    d = request.get_json(); hashed = generate_password_hash(d.get('password'))
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO system_users (school_id, email, password_hash, role, linked_student_id) VALUES (%s, %s, %s, %s, %s)", (current_user.school_id, d.get('email'), hashed, d.get('role'), d.get('linked_student_id') or None))
        conn.commit(); return jsonify({"message": "Account created!"}), 201
    except: return jsonify({"error": "Email exists."}), 409
    finally: cur.close(); conn.close()

# --- 8. THE FRONTEND DASHBOARD ---
@app.route('/dashboard')
def dashboard():
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Ghana SMS | ERP</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root { --primary: #0f4c81; --secondary: #f4f7f6; --accent: #28a745; --text: #333; --danger: #dc3545; --info: #17a2b8; --warning: #ffc107;}
            body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--secondary); margin: 0; display: flex; color: var(--text); }
            .sidebar { width: 250px; background: var(--primary); color: white; min-height: 100vh; padding: 20px; box-sizing: border-box; position: fixed; overflow-y: auto;}
            .sidebar h2 { margin-top: 0; border-bottom: 1px solid rgba(255,255,255,0.2); padding-bottom: 10px; font-size: 1.2rem; }
            .sidebar button { background: rgba(255,255,255,0.1); color: white; border: none; padding: 12px; width: 100%; text-align: left; margin-bottom: 5px; border-radius: 4px; cursor: pointer; transition: 0.3s; }
            .sidebar button:hover { background: rgba(255,255,255,0.2); }
            .main-content { margin-left: 250px; flex: 1; padding: 40px; box-sizing: border-box; min-height: 100vh; }
            .card { background: white; padding: 25px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 20px; }
            h3 { margin-top: 0; color: var(--primary); border-bottom: 2px solid #eee; padding-bottom: 8px;}
            input, select, textarea { width: 100%; padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box;}
            .btn { background: var(--primary); color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; margin-bottom: 10px;}
            .btn-success { background: var(--accent); } .btn-danger { background: var(--danger); } .btn-info { background: var(--info); } .btn-warning { background: var(--warning); color: #333;}
            .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            .metric-box { padding: 15px; border-radius: 8px; color: white; text-align: center; }
            #toast { display: none; position: fixed; bottom: 30px; right: 30px; padding: 15px 25px; color: white; background: var(--accent); border-radius: 5px; z-index: 1000; font-weight: bold; }
            .hidden { display: none !important; }
        </style>
    </head>
    <body>
        <div id="toast">Message</div>
        <div class="sidebar">
            <h2>SaaS ERP Engine</h2>
            {% if current_user.is_authenticated %}
                <div style="font-size: 0.85rem; color: #a5c3e0; margin-bottom: 20px;">Role: <span style="text-transform: uppercase;">{{ current_user.role }}</span></div>
                {% if current_user.role == 'superadmin' %}
                    <button onclick="window.location.reload()">🏢 Global Tenants</button>
                    <button class="btn-success" onclick="sendAction('/api/setup_db', {}, true)">Sync Database</button>
                {% elif current_user.role == 'admin' %}
                    <button onclick="showSection('analytics-section')">📊 Dashboard</button>
                    <button onclick="showSection('finance-section')">💰 Financials</button>
                    <button onclick="showSection('admissions-section')">🎓 Admissions & IDs</button>
                    <button onclick="showSection('attendance-section')">📅 Roll Call</button>
                    <button onclick="showSection('academics-section')">📚 Academics</button>
                    <button onclick="showSection('sms-section')">📟 SMS Desk</button>
                    <button onclick="showSection('hr-section')">🧑‍🏫 Staff HR</button>
                {% endif %}
                <br><br><button class="btn-danger" onclick="logout()">Secure Logout</button>
            {% else %}
                <button class="btn-success" onclick="sendAction('/api/setup_db', {}, true)">1. Sync Database</button>
            {% endif %}
        </div>

        <main class="main-content">
            <h1>Platform Dashboard</h1>
            {% if not current_user.is_authenticated %}
            <div class="card" style="max-width: 400px; margin: 0 auto;">
                <h3>System Login</h3>
                <input type="email" id="email" placeholder="Email Address">
                <input type="password" id="pass" placeholder="Password">
                <button class="btn" onclick="login()">Login</button>
            </div>
            
            {% elif current_user.role == 'superadmin' %}
            <div class="card" style="border: 2px solid var(--accent);">
                <h3>🚀 Onboard New School</h3>
                <div class="grid-2">
                    <div><input type="text" id="onboardSchool" placeholder="School Name"><input type="email" id="onboardEmail" placeholder="Admin Email"></div>
                    <div><input type="password" id="onboardPass" placeholder="Admin Password"><button class="btn btn-success" onclick="onboardNewSchool()">Provision Tenant</button></div>
                </div>
            </div>
            <div class="card">
                <h3>Global Tenants</h3><button class="btn" onclick="loadSchools()">Refresh List</button><div id="school-container"></div>
            </div>

            {% elif current_user.role == 'admin' %}
            
            <!-- Dashboard Section -->
            <div id="analytics-section" class="card admin-section">
                <h3>Corporate Analytics</h3>
                <div class="grid-2" style="margin-bottom: 20px;">
                    <div class="metric-box" style="background: var(--primary);" id="metricRevenue">Gross Revenue: GHS 0.00</div>
                    <div class="metric-box" style="background: var(--danger);" id="metricExpenses">Total Expenses: GHS 0.00</div>
                    <div class="metric-box" style="background: var(--accent); grid-column: span 2;" id="metricMargin">Net Margin: GHS 0.00</div>
                </div>
                <div class="grid-2">
                    <div style="position: relative; height: 250px;"><canvas id="financeChart"></canvas></div>
                    <div style="position: relative; height: 250px;"><canvas id="waecChart"></canvas></div>
                </div>
            </div>

            <!-- Financials Section -->
            <div id="finance-section" class="card admin-section hidden" style="border: 2px solid var(--danger);">
                <h3>💰 Financial Ledger & Operations</h3>
                <div class="grid-2">
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px;">
                        <h4 style="margin-top:0;">1. Issue Segmented Bill</h4>
                        <input type="number" id="bStuId" placeholder="Student ID (Required)">
                        <select id="bCat">
                            <option value="Consolidated Fee">Consolidated Term Fee (All-Inclusive)</option>
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
                        <button class="btn btn-success" onclick="sendAction('/api/fees/bill', {student_id: document.getElementById('bStuId').value, fee_category: document.getElementById('bCat').value, amount_due: document.getElementById('bAmount').value, academic_year: document.getElementById('bYear').value, term: document.getElementById('bTerm').value, description: document.getElementById('bDesc').value})">Issue Bill</button>
                    </div>

                    <div>
                        <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 15px;">
                            <h4 style="margin-top:0;">2. Record Payment</h4>
                            <div class="grid-2">
                                <input type="number" id="pFeeId" placeholder="Fee ID">
                                <input type="number" id="pAmount" placeholder="Amount (GHS)">
                            </div>
                            <select id="pMethod"><option value="Cash">Cash</option><option value="Mobile Money (MoMo)">Mobile Money</option></select>
                            <button class="btn btn-success" onclick="sendAction('/api/fees/pay', {fee_id: document.getElementById('pFeeId').value, amount_paid: document.getElementById('pAmount').value, payment_method: document.getElementById('pMethod').value})">Log Payment</button>
                        </div>
                        <div style="background: #fff3cd; padding: 15px; border-radius: 8px; border: 1px solid var(--warning);">
                            <h4 style="margin-top:0;">3. Log Operational Expense</h4>
                            <select id="eCat"><option value="Staff Salaries">Staff Salaries</option><option value="Boarding Provisions">Boarding Provisions</option><option value="Utilities">Utilities</option></select>
                            <div class="grid-2"><input type="text" id="eDesc" placeholder="Desc"><input type="number" id="eAmount" placeholder="Amount"></div>
                            <button class="btn btn-danger" onclick="sendAction('/api/expenses', {category: document.getElementById('eCat').value, description: document.getElementById('eDesc').value, amount: document.getElementById('eAmount').value})">Log Outflow</button>
                            <button class="btn btn-info" style="margin-bottom:0;" onclick="loadExpenses()">View Expenses</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Admissions Section -->
            <div id="admissions-section" class="card grid-2 admin-section hidden">
                <div>
                    <h3>Enroll New Student</h3>
                    <div class="grid-2"><input type="text" id="sFirst" placeholder="First Name"><input type="text" id="sLast" placeholder="Last Name"></div>
                    <div class="grid-2">
                        <select id="sBoarding"><option value="Day">Day Student</option><option value="Boarding">Boarding Student</option></select>
                        <input type="text" id="sHouse" placeholder="House (e.g. Aggrey House)">
                    </div>
                    <div class="grid-2"><input type="text" id="sGName" placeholder="Guardian Name"><input type="text" id="sGContact" placeholder="Contact"></div>
                    <div style="margin-bottom: 15px;">
                        <label style="font-size:0.8rem; font-weight:bold;">Passport Photo (Auto-Compressing)</label>
                        <input type="file" id="sPhoto" accept="image/*">
                    </div>
                    <button class="btn btn-success" onclick="enrollStudent()">Register Student</button>
                </div>
                <div>
                    <h3>ID & Directory Tools</h3>
                    <button class="btn btn-warning" onclick="window.open('/print_ids', '_blank')">🖨️ Generate Batch ID Cards</button>
                    <hr style="margin:20px 0; border:1px solid #eee;">
                    <button class="btn" onclick="loadRoster()">View Digital Directory</button>
                </div>
            </div>

            <!-- Attendance Section -->
            <div id="attendance-section" class="card admin-section hidden" style="border: 2px solid var(--info);">
                <h3>📅 Daily Roll Call & Feeding Optimization</h3>
                <div class="grid-2">
                    <div>
                        <input type="date" id="attDate" value="">
                        <input type="number" id="attStuId" placeholder="Student ID">
                    </div>
                    <div>
                        <select id="attStatus">
                            <option value="Present">Present (Include in Feeding)</option>
                            <option value="Absent">Absent (Remove from Feeding)</option>
                        </select>
                        <button class="btn btn-info" onclick="sendAction('/api/attendance', {student_id: document.getElementById('attStuId').value, record_date: document.getElementById('attDate').value, status: document.getElementById('attStatus').value})">Mark Attendance</button>
                    </div>
                </div>
            </div>

            <!-- Academics Section -->
            <div id="academics-section" class="card grid-2 admin-section hidden">
                <div>
                    <h3>Record SBA Grade (30/70)</h3>
                    <div class="grid-2">
                        <input type="number" id="gStuId" placeholder="Student ID (Required)">
                        <input type="text" id="gSub" placeholder="Subject">
                    </div>
                    <div class="grid-2">
                        <input type="number" id="gClass" placeholder="Class Score (30%)">
                        <input type="number" id="gExam" placeholder="Exam Score (70%)">
                    </div>
                    <div class="grid-2">
                        <input type="text" id="gTerm" placeholder="Term (e.g. Term 1)">
                        <input type="text" id="gYear" placeholder="Year (e.g. 2026)">
                    </div>
                    <input type="text" id="gRem" placeholder="Teacher's Remark (e.g. Very impressive)">
                    <button class="btn" onclick="sendAction('/api/grades', {student_id: document.getElementById('gStuId').value, subject_name: document.getElementById('gSub').value, class_score: document.getElementById('gClass').value, exam_score: document.getElementById('gExam').value, term: document.getElementById('gTerm').value, academic_year: document.getElementById('gYear').value, remarks: document.getElementById('gRem').value})">Save SBA Record</button>
                </div>
                <div>
                    <h3>Terminal Reports</h3>
                    <input type="number" id="repId" placeholder="Student ID">
                    <button class="btn btn-success" onclick="loadReport()">View Terminal Report Card</button>
                    <hr style="margin:20px 0; border:1px solid #eee;">
                    <button class="btn btn-info" onclick="loadStatement()">View Financial Statement</button>
                </div>
            </div>

            <!-- SMS Desk Section -->
            <div id="sms-section" class="card admin-section hidden" style="border: 2px solid var(--info);">
                <h3>📟 SMS Communication Desk</h3>
                <div class="grid-2">
                    <div>
                        <label style="font-weight:bold; display:block; margin-bottom:5px;">Target Audience</label>
                        <select id="smsAudience">
                            <option value="all">Broadcast to All Parents</option>
                            <option value="arrears">Only Parents with Unpaid Arrears</option>
                            <option value="boarding">Parents of Boarding Students</option>
                        </select>
                        <p style="font-size:0.8rem; color:#666;">The system will automatically extract contact numbers from the database based on your selection.</p>
                    </div>
                    <div>
                        <label style="font-weight:bold; display:block; margin-bottom:5px;">Message Content</label>
                        <textarea id="smsBody" placeholder="Enter your text message here..."></textarea>
                        <button class="btn btn-info" onclick="sendAction('/api/sms/blast', {audience: document.getElementById('smsAudience').value, message: document.getElementById('smsBody').value})">Send SMS Broadcast</button>
                    </div>
                </div>
            </div>

            <!-- Staff HR Section -->
            <div id="hr-section" class="card grid-2 admin-section hidden">
                <div>
                    <h3>Register Staff Profile</h3>
                    <input type="email" id="tEmail" placeholder="Teacher Email">
                    <input type="password" id="tPass" placeholder="Temporary Password">
                    <button class="btn" onclick="sendAction('/api/register_staff', {email: document.getElementById('tEmail').value, password: document.getElementById('tPass').value, role: 'teacher'})">Add to HR Directory</button>
                </div>
                <div>
                    <h3>Compliance Vault</h3>
                    <p style="font-size: 0.9rem; color: #666;">Maintain digital records of staff to ensure instant readiness for GES auditing.</p>
                </div>
            </div>

            <!-- Shared Data Viewer -->
            <div class="card hidden" id="data-viewer" style="border: 2px solid var(--primary);">
                <h3 id="viewer-title">Data Explorer</h3>
                <div id="table-container"></div>
            </div>
            {% endif %}
        </main>

        <script>
            function showSection(sectionId) {
                const sections = ['analytics-section', 'finance-section', 'admissions-section', 'attendance-section', 'academics-section', 'sms-section', 'hr-section'];
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

            function showToast(message, isError=false) {
                const toast = document.getElementById('toast');
                toast.innerText = message;
                toast.style.background = isError ? '#dc3545' : '#28a745';
                toast.style.display = 'block';
                setTimeout(() => { toast.style.display = 'none'; }, 5000);
            }

            async function login() {
                const res = await fetch('/api/login', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email: document.getElementById('email').value, password: document.getElementById('pass').value})
                });
                const data = await res.json();
                if (res.ok) { showToast(data.message); setTimeout(() => window.location.reload(), 1000); } 
                else { showToast(data.error, true); }
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
                    guardian_name: document.getElementById('sGName').value,
                    guardian_contact: document.getElementById('sGContact').value,
                    boarding_status: document.getElementById('sBoarding').value,
                    house: document.getElementById('sHouse').value,
                    photo_b64: photo_b64
                };
                sendAction('/api/students', payload);
            }

            async function loadSchools() {
                const res = await fetch('/api/superadmin/schools');
                if(!res.ok) return;
                const data = await res.json();
                let html = '<table style="width:100%; border-collapse: collapse; text-align: left;"><tr><th style="padding:10px; background:#0f4c81; color:white;">ID</th><th style="padding:10px; background:#0f4c81; color:white;">School Name</th><th style="padding:10px; background:#0f4c81; color:white;">Expiry Date</th><th style="padding:10px; background:#0f4c81; color:white;">Status</th><th style="padding:10px; background:#0f4c81; color:white;">Actions</th></tr>';
                data.data.forEach(s => {
                    const statusColor = s.status === 'Active' ? 'green' : 'red';
                    html += `<tr>
                        <td style="padding:10px; border-bottom:1px solid #ddd;">${s.school_id}</td>
                        <td style="padding:10px; border-bottom:1px solid #ddd;">${s.school_name}</td>
                        <td style="padding:10px; border-bottom:1px solid #ddd;">${s.expiry_date}</td>
                        <td style="color:${statusColor}; font-weight:bold; padding:10px; border-bottom:1px solid #ddd;">${s.status}</td>
                        <td style="padding:10px; border-bottom:1px solid #ddd;">
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

            function renderTable(title, headers, rows, keys) {
                const viewer = document.getElementById('data-viewer');
                document.getElementById('viewer-title').innerText = title;
                const container = document.getElementById('table-container');
                if (!rows || rows.length === 0) { container.innerHTML = '<div style="padding: 20px;">No records found.</div>'; viewer.classList.remove('hidden'); return; }
                let html = '<table style="width:100%; border-collapse: collapse;"><tr>';
                headers.forEach(h => html += `<th style="background:#0f4c81;color:white;padding:10px;text-align:left;">${h}</th>`);
                html += '</tr>';
                rows.forEach(row => {
                    html += '<tr>';
                    keys.forEach(k => {
                        let val = row[k];
                        if (k === 'photo') {
                            html += `<td style="padding:10px; border-bottom:1px solid #ddd; width: 60px;"><img src="/api/photo/${row['student_id']}" style="width:45px; height:45px; border-radius:50%; object-fit:cover; border:2px solid #ccc; background:#eee;"></td>`;
                        } else if (k === 'remaining_balance' && val > 0) {
                            html += `<td style="color:#dc3545; font-weight:bold; padding:10px; border-bottom:1px solid #ddd;">${val}</td>`;
                        } else if (k === 'boarding_status') {
                            let badge = val === 'Boarding' ? 'background:#0f4c81;color:white;' : 'background:#eee;color:black;';
                            html += `<td style="padding:10px; border-bottom:1px solid #ddd;"><span style="${badge}padding:3px 8px;border-radius:12px;font-size:0.8rem;">${val}</span></td>`;
                        } else {
                            html += `<td style="padding:10px; border-bottom:1px solid #ddd;">${val}</td>`;
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
                renderTable("Student Roster", ['Photo', 'ID', 'First Name', 'Last Name', 'Status', 'House', 'Guardian Contact'], data.data, ['photo', 'student_id', 'first_name', 'last_name', 'boarding_status', 'house', 'guardian_contact']);
            }
            async function loadExpenses() {
                const res = await fetch('/api/expenses'); const data = await res.json();
                renderTable("Expense Ledger", ['Date', 'Category', 'Description', 'Amount (GHS)'], data.data, ['date', 'category', 'description', 'amount']);
            }
            async function loadReport() {
                const id = document.getElementById('repId').value;
                const res = await fetch('/api/report_card/' + id);
                if (!res.ok) { showToast("Not Found", true); return; }
                const data = await res.json();
                renderTable("Terminal Report Card", ['Subject', 'Class', 'Exam', 'Total', 'Grade', 'Remarks', 'Term'], data.grades, ['subject_name', 'class_score', 'exam_score', 'total_score', 'waec_grade', 'teacher_remarks', 'term']);
            }
            async function loadStatement() {
                const id = document.getElementById('repId').value;
                const res = await fetch('/api/statement/' + id);
                if (!res.ok) { showToast("Not Found", true); return; }
                const data = await res.json();
                renderTable("Segmented Ledger", ['Fee ID', 'Category', 'Year', 'Term', 'Due', 'Paid', 'Arrears'], data.statement, ['fee_id', 'fee_category', 'academic_year', 'term', 'amount_due', 'total_paid', 'remaining_balance']);
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
                            type: 'bar', data: { labels: labels, datasets: [{ label: 'Students', data: counts, backgroundColor: '#0f4c81' }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { title: { display: true, text: 'WAEC Grade Distribution' } } }
                        });
                    } catch(e) {}
                }
            }
            window.onload = loadDashboardData;
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, current_user=current_user)

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
