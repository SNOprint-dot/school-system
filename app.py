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
    
    # --- NEW MODULE TABLES ---
    cur.execute("CREATE TABLE IF NOT EXISTS transport_routes (route_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, route_name VARCHAR(100) NOT NULL, driver_name VARCHAR(100), fare DECIMAL(10, 2) DEFAULT 0.00)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_items (item_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, item_name VARCHAR(100) NOT NULL, price DECIMAL(10, 2) NOT NULL, stock INTEGER DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory_sales (sale_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, item_name VARCHAR(100), quantity INTEGER, total_cost DECIMAL(10, 2), sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS exeats (exeat_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, exeat_type VARCHAR(50), reason VARCHAR(255), expected_return DATE, status VARCHAR(20) DEFAULT 'Active', issue_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS sick_bay_logs (log_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, symptoms VARCHAR(255), treatment VARCHAR(255), log_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS academic_calendar (event_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, event_title VARCHAR(150), event_date DATE, description VARCHAR(255))")

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

# --- ALL 5 NEW ENTERPRISE MODULES ---

# 1. TRANSPORT MODULE
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

# 2. INVENTORY & SCHOOL STORE MODULE
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
    d = request.get_json()
    item_id = int(d.get('item_id')); qty = int(d.get('quantity')); stu_id = int(d.get('student_id'))
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

# 3. EXEAT & BOARDING MODULE (WITH LIVE SMS)
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
            
            # Auto-SMS to Parent
            api_key = os.environ.get('SMS_API_KEY'); sender = os.environ.get('SMS_SENDER_ID', 'SMS_ADMIN'); sms_msg = ""
            if api_key and stu['guardian_contact']:
                try:
                    msg = f"ALERT: Your ward {stu['first_name']} has been issued an Exeat ({d.get('exeat_type')}) for: {d.get('reason')}. Expected return: {d.get('expected_return')}."
                    requests.post("https://sms.arkesel.com/api/v2/sms/send", json={"sender": sender, "message": msg, "recipients": [stu['guardian_contact']]}, headers={"api-key": api_key})
                    sms_msg = " Parent notified via SMS."
                except Exception: sms_msg = " SMS failed."
            return jsonify({"message": f"Exeat issued securely.{sms_msg}"}), 201
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

# 4. SICK BAY MODULE
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
            conn.commit()
            
            sms_msg = ""
            if d.get('alert_parent'):
                cur.execute("SELECT first_name, guardian_contact FROM students WHERE student_id = %s AND school_id = %s", (stu_id, current_user.school_id))
                stu = cur.fetchone()
                api_key = os.environ.get('SMS_API_KEY'); sender = os.environ.get('SMS_SENDER_ID', 'SMS_ADMIN')
                if stu and api_key and stu['guardian_contact']:
                    try:
                        msg = f"MEDICAL ALERT: Your ward {stu['first_name']} visited the Sick Bay today. Symptoms: {d.get('symptoms')}. Please contact the school."
                        requests.post("https://sms.arkesel.com/api/v2/sms/send", json={"sender": sender, "message": msg, "recipients": [stu['guardian_contact']]}, headers={"api-key": api_key})
                        sms_msg = " Emergency SMS sent to parent."
                    except Exception: sms_msg = " SMS failed."
            return jsonify({"message": f"Medical record saved.{sms_msg}"}), 201
        except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
        finally: cur.close(); conn.close()
    else:
        cur.execute("SELECT sb.log_id, sb.student_id, s.first_name, s.last_name, sb.symptoms, sb.treatment, TO_CHAR(sb.log_date, 'YYYY-MM-DD HH24:MI') as time FROM sick_bay_logs sb JOIN students s ON sb.student_id = s.student_id WHERE sb.school_id = %s ORDER BY sb.log_date DESC", (current_user.school_id,))
        logs = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": logs})

# 5. ACADEMIC CALENDAR MODULE
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

# --- OLD TENANT ENDPOINTS (ENROLLMENT & DIRECTORY) ---
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

# --- OLD REPORT, ANALYTICS, SETTINGS ROUTES CONTINUED UNMODIFIED BELOW ---
@app.route('/api/students/bulk', methods=['POST'])
@login_required
def bulk_enroll():
    if current_user.role != 'admin': return jsonify({"error": "Admin only"}), 403
    if 'file' not in request.files: return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    try:
        stream = StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.reader(stream); next(csv_input, None) 
        conn = get_db_connection(); cur = conn.cursor(); count = 0
        for row in csv_input:
            if len(row) >= 2: 
                fname = row[0].strip()[:100]; lname = row[1].strip()[:100]; c_class = row[2].strip()[:100] if len(row) > 2 and row[2].strip() else 'Unassigned'
                g_name = row[3].strip()[:100] if len(row) > 3 and row[3].strip() else 'N/A'; g_contact = row[4].strip()[:20] if len(row) > 4 and row[4].strip() else ''
                b_status = row[5].strip()[:20] if len(row) > 5 and row[5].strip() else 'Day'; house = row[6].strip()[:100] if len(row) > 6 and row[6].strip() else 'Unassigned'
                cur.execute("INSERT INTO students (school_id, first_name, last_name, current_class, guardian_name, guardian_contact, boarding_status, house) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            (current_user.school_id, fname, lname, c_class, g_name, g_contact, b_status, house))
                count += 1
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Bulk CSV Enrollment', f"Enrolled {count} students"))
        conn.commit(); return jsonify({"message": f"Bulk Upload Success! Enrolled {count} students."}), 201
    except Exception as e: return jsonify({"error": f"Upload failed. Ensure CSV format is correct. Error: {str(e)}"}), 500
    finally:
        if 'cur' in locals(): cur.close(); conn.close()

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

@app.route('/print_report/<int:student_id>', methods=['GET'])
@login_required
def print_report(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return "Access Denied.", 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
    student = cur.fetchone()
    if not student: return "Student record not found or access denied.", 404
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone(); primary_color = inst['primary_color'] or '#0f4c81'
    cur.execute("SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.teacher_remarks, g.term, g.academic_year FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s AND g.school_id = %s ORDER BY g.academic_year DESC, g.term DESC, sub.subject_name", (student_id, current_user.school_id))
    grades = cur.fetchall(); cur.close(); conn.close()

    table_rows = ""
    for g in grades: table_rows += f"<tr><td>{g['subject_name']}</td><td>{g['class_score']}</td><td>{g['exam_score']}</td><td><strong>{g['total_score']}</strong></td><td><strong>{g['waec_grade']}</strong></td><td>{g['teacher_remarks']}</td><td>{g['term']} ({g['academic_year']})</td></tr>"
    if not table_rows: table_rows = "<tr><td colspan='7' style='text-align:center;'>No academic records found for this student.</td></tr>"

    html = f"""<!DOCTYPE html><html><head><title>Terminal Report | {student['first_name']} {student['last_name']}</title><style>:root {{ --primary: {primary_color}; }} body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #eee; padding: 20px; color: #333; }} .page {{ background: white; max-width: 900px; margin: auto; padding: 40px; box-shadow: 0 0 15px rgba(0,0,0,0.1); border-radius: 8px; }} .header {{ display: flex; justify-content: space-between; border-bottom: 3px solid var(--primary); padding-bottom: 20px; margin-bottom: 30px; }} .school-name {{ color: var(--primary); font-size: 28px; font-weight: bold; margin: 0 0 5px 0; text-transform: uppercase; }} .student-details {{ font-size: 16px; line-height: 1.8; margin-top: 15px; }} .photo {{ width: 120px; height: 140px; border: 2px solid var(--primary); object-fit: cover; border-radius: 5px; }} table {{ width: 100%; border-collapse: collapse; margin-bottom: 40px; }} th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; font-size: 14px; }} th {{ background: var(--primary); color: white; text-transform: uppercase; font-size: 13px; }} tr:nth-child(even) {{ background-color: #f9f9f9; }} .signatures {{ display: flex; justify-content: space-between; margin-top: 80px; padding: 0 20px; }} .sig-line {{ border-top: 2px solid #333; width: 250px; text-align: center; padding-top: 10px; font-weight: bold; font-size: 14px; text-transform: uppercase; }} .btn-print {{ padding:12px 25px; margin-bottom:20px; cursor:pointer; background:#28a745; color:white; border:none; border-radius:8px; font-weight:bold; font-size: 16px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }} @media print {{ body {{ background: white; padding: 0; }} .page {{ box-shadow: none; max-width: 100%; padding: 0; }} .no-print {{ display: none; }} }}</style></head><body><div style="text-align: center;"><button class="no-print btn-print" onclick="window.print()">🖨️ Print Official Report</button></div><div class="page"><div class="header"><div style="display:flex; gap: 20px;"><img src="/api/logo/{current_user.school_id}" style="width:90px; height:90px; object-fit:contain;"><div><h1 class="school-name">{inst['school_name']}</h1><div style="font-size:12px; color:#666; margin-bottom:15px;">{inst['address']} | Tel: {inst['phone']}</div><h2 style="margin: 0 0 10px 0; color: #555;">OFFICIAL TERMINAL REPORT</h2></div></div></div><div style="display:flex; justify-content: space-between; align-items: flex-end; margin-bottom: 25px;"><div class="student-details"><strong>STUDENT NAME:</strong> {student['first_name'].upper()} {student['last_name'].upper()}<br><strong>STUDENT ID:</strong> {inst['school_name'][:3].upper()}-{student['student_id']:04d}<br><strong>CURRENT CLASS:</strong> {student['current_class'].upper()}<br><strong>BOARDING STATUS:</strong> {student['boarding_status'].upper()}</div><img src="/api/photo/{student['student_id']}" class="photo"></div><table><tr><th>Subject</th><th>Class (30%)</th><th>Exam (70%)</th><th>Total (100%)</th><th>Grade</th><th>Teacher's Remarks</th><th>Academic Term</th></tr>{table_rows}</table><div class="signatures"><div class="sig-line">Class Teacher's Signature</div><div class="sig-line">Headmaster's Signature</div></div></div></body></html>"""
    return html

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

# --- 8. THE FRONTEND DASHBOARD WITH ALL 5 MODULES ---
@app.route('/dashboard')
def dashboard():
    school_name = "Global ERP Engine"; primary_color = "#0f4c81"
    if current_user.is_authenticated and current_user.school_id:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT school_name, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
        inst = cur.fetchone(); cur.close(); conn.close()
        if inst: school_name = inst['school_name']; primary_color = inst['primary_color'] or "#0f4c81"

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
            .sidebar button { background: transparent; color: rgba(255,255,255,0.85); border: none; padding: 10px 15px; width: 100%; text-align: left; margin-bottom: 5px; border-radius: 8px; cursor: pointer; transition: all 0.2s ease; font-size: 0.95rem; font-weight: 500;}
            .sidebar button:hover { background: rgba(255,255,255,0.15); color: white; padding-left: 20px;}
            
            .main-content { margin-left: 260px; flex: 1; padding: 40px; box-sizing: border-box; min-height: 100vh; }
            
            .card { background: white; padding: 30px; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.04); border: 1px solid #eaeaea; margin-bottom: 25px; transition: transform 0.2s ease;}
            h3 { margin-top: 0; color: var(--primary); border-bottom: 2px solid #f0f0f0; padding-bottom: 10px; margin-bottom: 20px;}
            
            input, select, textarea { width: 100%; padding: 12px; margin-bottom: 15px; border: 1px solid #ced4da; border-radius: 8px; box-sizing: border-box; transition: all 0.2s ease; font-family: inherit;}
            input:focus, select:focus, textarea:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(0,0,0,0.05); }
            
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
                    <button onclick="secureSection('finance-section')">💰 Financials & Billing 🔒</button>
                    <button onclick="showSection('admissions-section')">🎓 Admissions & Directory</button>
                    <button onclick="showSection('attendance-section')">📅 Roll Call & Feeding</button>
                    <button onclick="showSection('academics-section')">📚 Academic Reporting</button>
                    
                    <!-- NEW ENTERPRISE MODULES -->
                    <button onclick="showSection('transport-section')">🚌 Transport Logistics</button>
                    <button onclick="showSection('store-section')">📦 School Store & POS</button>
                    <button onclick="showSection('exeat-section')">🎫 Boarding & Exeats</button>
                    <button onclick="showSection('sickbay-section')">🏥 Sick Bay Log</button>
                    <button onclick="showSection('calendar-section')">📅 Academic Calendar</button>
                    
                    <button onclick="showSection('sms-section')">📟 Live SMS Gateway</button>
                    <button onclick="showSection('hr-section')">🧑‍🏫 Staff HR & Portals</button>
                    <button onclick="secureSection('settings-section')">⚙️ Security & Settings 🔒</button>
                
                {% elif current_user.role == 'teacher' %}
                    <button onclick="showSection('attendance-section')">📅 Daily Roll Call</button>
                    <button onclick="showSection('academics-section')">📚 SBA Grading Matrix</button>
                    <button onclick="showSection('calendar-section')">📅 Academic Calendar</button>
                    <button onclick="showSection('settings-section')">⚙️ Account Security</button>
                
                {% elif current_user.role == 'guardian' %}
                    <button onclick="showSection('guardian-section')">👨‍👩‍👧 Guardian Portal</button>
                    <button onclick="showSection('calendar-section')">📅 Academic Calendar</button>
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
                    <div style="background: #e9ecef; padding: 25px; border-radius: 12px; text-align: center;">
                        <h4 style="margin-top:0; color: var(--primary);">Academic Performance</h4>
                        <button class="btn btn-success" style="width:auto;" onclick="window.open('/print_report/{{ current_user.linked_student_id }}', '_blank')">🖨️ View & Print Terminal Report</button>
                    </div>
                    <div style="background: #fff3cd; padding: 25px; border-radius: 12px; text-align: center; border: 1px solid var(--warning);">
                        <h4 style="margin-top:0; color: #856404;">Financial Statement</h4>
                        <button class="btn btn-warning" style="width:auto; color: #333;" onclick="loadStatement({{ current_user.linked_student_id }})">View Financial Ledger</button>
                    </div>
                </div>
            </div>

            {% elif current_user.role == 'superadmin' %}
            <div id="sa-settings-section" class="card admin-section hidden" style="border: 2px solid #333;">
                <h3>⚙️ Master Settings & Credentials</h3>
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
                <input type="hidden" id="brandSchoolId">
                <div class="grid-2">
                    <div><label>Primary Theme Color</label><input type="color" id="brandColor" style="height: 50px; cursor:pointer;"></div>
                    <div><label>School Crest / Logo Upload</label><input type="file" id="brandLogo" accept="image/png, image/jpeg" style="background:white;"></div>
                </div>
                <div style="display:flex; gap: 10px; margin-top: 10px;">
                    <button class="btn btn-warning" style="color:#333;" onclick="saveBranding()">Apply Theme & Refresh</button>
                    <button class="btn" style="background:#666;" onclick="document.getElementById('branding-div').classList.add('hidden')">Cancel</button>
                </div>
            </div>
            <div class="card" style="border: 2px solid var(--danger);">
                <h3>🔑 Super Admin Master Override</h3>
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
                                <select id="bCat"><option value="Consolidated Fee">Consolidated Term Fee</option><option value="Tuition">Tuition Fee</option><option value="PTA Dues">PTA Dues</option><option value="Feeding Fee">Feeding Fee</option><option value="Exams Fee">Exams Fee</option><option value="Arrears (Past Term)">Arrears (Past Term)</option></select>
                                <div class="grid-2"><input type="text" id="bTerm" placeholder="Term"><input type="text" id="bYear" placeholder="Year"></div>
                                <input type="number" id="bAmount" placeholder="Amount Due (GHS)">
                                <input type="text" id="bDesc" placeholder="Description / Memo">
                                <button class="btn btn-success" onclick="issueBill()">Issue Bill & View Ledger</button>
                            </div>
                            <div style="background: #e2e3e5; padding: 20px; border-radius: 12px; border: 1px solid #ccc;">
                                <h4 style="margin-top:0; color:#333;">⚡ Automated Bulk Class Billing</h4>
                                <input type="text" id="bbClass" placeholder="Target Class (e.g., Basic 3)">
                                <select id="bbCat"><option value="Consolidated Fee">Consolidated Term Fee</option><option value="Tuition">Tuition Fee</option><option value="PTA Dues">PTA Dues</option></select>
                                <div class="grid-2"><input type="text" id="bbTerm" placeholder="Term"><input type="text" id="bbYear" placeholder="Year"></div>
                                <input type="number" id="bbAmount" placeholder="Amount Due (GHS)">
                                <input type="text" id="bbDesc" placeholder="Memo / Description">
                                <button class="btn btn-primary" onclick="bulkBillClass()">Execute Bulk Billing Protocol</button>
                            </div>
                        </div>
                        <div>
                            <div style="background: #e9ecef; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #dee2e6;">
                                <h4 style="margin-top:0; color: var(--primary);">2. Student Ledger & Payments</h4>
                                <div class="grid-2" style="align-items: center;"><input type="number" id="stateStuId" placeholder="Target Student ID" style="margin-bottom:0;"><button class="btn" style="margin-bottom:0;" onclick="loadStatement()">Open Secure Ledger</button></div>
                            </div>
                            <div style="background: #fff3cd; padding: 20px; border-radius: 12px; border: 1px solid var(--warning); margin-bottom: 20px;">
                                <h4 style="margin-top:0; color: #856404;">3. Live Arrears & Debtors Radar</h4>
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
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px;">Enroll New Student</h3>
                            <div class="grid-2"><input type="text" id="sFirst" placeholder="First Name"><input type="text" id="sLast" placeholder="Last Name"></div>
                            <div class="grid-2">
                                <input type="text" id="sClass" placeholder="Class / Program">
                                <select id="sBoarding"><option value="Day">Day Student</option><option value="Boarding">Boarding Student</option></select>
                            </div>
                            <div class="grid-2">
                                <input type="text" id="sHouse" placeholder="House (or N/A)">
                                <input type="text" id="sRoute" placeholder="Bus Route (or None)">
                            </div>
                            <div class="grid-2" style="margin-bottom: 15px;">
                                <input type="text" id="sGName" placeholder="Guardian Name">
                                <input type="text" id="sGContact" placeholder="Guardian Contact">
                            </div>
                            <label style="font-size:0.8rem; font-weight:bold; display:block; margin-bottom: 5px;">Passport Photo</label>
                            <input type="file" id="sPhoto" accept="image/*">
                            <button class="btn btn-success" onclick="enrollStudent()">Register Student</button>
                        </div>
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #dee2e6;">
                            <h4 style="margin-top:0; color: var(--primary);">Bulk CSV Enrollment Engine</h4>
                            <div style="display:flex; gap:10px; align-items:center;">
                                <input type="file" id="csvUpload" accept=".csv" style="margin-bottom:0; background:white;">
                                <button class="btn btn-primary" style="margin-bottom:0; width:60%;" onclick="bulkEnrollCSV()">Import Roster</button>
                            </div>
                        </div>
                    </div>
                    <div>
                        <div style="background: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #eee; box-shadow: 0 4px 6px rgba(0,0,0,0.02);">
                            <h3 style="margin-top:0; border:none; padding:0; margin-bottom:15px;">Directory Tools</h3>
                            <div class="grid-2">
                                <button class="btn btn-warning" style="color:#333;" onclick="window.open('/print_ids', '_blank')">🖨️ Print Batch IDs</button>
                                <button class="btn btn-info" onclick="loadRoster()">View Directory</button>
                            </div>
                        </div>
                        <div style="background: #fff3cd; padding: 20px; border-radius: 12px; border: 1px solid var(--warning); margin-bottom: 20px;">
                            <h4 style="margin-top:0; color: #856404;">✏️ Data Correction Editor</h4>
                            <input type="number" id="editStuId" placeholder="Target Student ID">
                            <div class="grid-2"><input type="text" id="editFirst" placeholder="Corrected First Name"><input type="text" id="editLast" placeholder="Corrected Last Name"></div>
                            <div class="grid-2"><input type="text" id="editClass" placeholder="Corrected Class"><select id="editBoarding"><option value="Day">Day Student</option><option value="Boarding">Boarding Student</option></select></div>
                            <button class="btn btn-warning" onclick="editStudent()" style="color:#333;">Save Corrections to Record</button>
                        </div>
                        <div style="background: #e2e3e5; padding: 20px; border-radius: 12px; border: 1px solid #ccc;">
                            <h4 style="margin-top:0; color: #333;">End-of-Year Promotion Engine</h4>
                            <div class="grid-2"><input type="text" id="promoFrom" placeholder="Current Class"><input type="text" id="promoTo" placeholder="Next Class"></div>
                            <button class="btn btn-primary" onclick="promoteClass()">Promote Entire Cohort</button>
                        </div>
                    </div>
                </div>

                <!-- 1. TRANSPORT MODULE -->
                <div id="transport-section" class="card grid-2 admin-section hidden" style="border-top: 5px solid #fd7e14;">
                    <div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; border: 1px solid #eee;">
                            <h3 style="margin-top:0; color:#fd7e14;">🚌 Create Bus Route</h3>
                            <input type="text" id="trName" placeholder="Route Name (e.g. Zone A - East Legon)">
                            <input type="text" id="trDriver" placeholder="Driver Name">
                            <input type="number" id="trFare" placeholder="Term Fare (GHS)">
                            <button class="btn" style="background:#fd7e14;" onclick="sendAction('/api/transport', {route_name: document.getElementById('trName').value, driver_name: document.getElementById('trDriver').value, fare: document.getElementById('trFare').value})">Add Bus Route</button>
                        </div>
                    </div>
                    <div>
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #ccc; height:100%; box-sizing:border-box;">
                            <h3 style="margin-top:0; color:#333;">Transport Logistics</h3>
                            <p style="font-size:0.9rem; color:#555;">To assign a student to a bus route, use the <b>Admissions</b> tab (Edit Data Correction tool) to update their Transport Route field.</p>
                            <button class="btn btn-info" onclick="loadTransport()">View All Active Routes</button>
                        </div>
                    </div>
                </div>

                <!-- 2. INVENTORY STORE MODULE -->
                <div id="store-section" class="card grid-2 admin-section hidden" style="border-top: 5px solid #6f42c1;">
                    <div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; border: 1px solid #eee; margin-bottom: 20px;">
                            <h3 style="margin-top:0; color:#6f42c1;">📦 Add to Inventory Catalog</h3>
                            <input type="text" id="invName" placeholder="Item Name (e.g. PE Kit - Size M)">
                            <div class="grid-2">
                                <input type="number" id="invPrice" placeholder="Price (GHS)">
                                <input type="number" id="invStock" placeholder="Initial Stock Qty">
                            </div>
                            <button class="btn" style="background:#6f42c1;" onclick="sendAction('/api/inventory', {item_name: document.getElementById('invName').value, price: document.getElementById('invPrice').value, stock: document.getElementById('invStock').value})">Add Item to Catalog</button>
                        </div>
                        <button class="btn btn-info" onclick="loadInventory()">View Store Catalog</button>
                    </div>
                    <div>
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #ccc; height:100%; box-sizing:border-box;">
                            <h3 style="margin-top:0; color:#333;">🛒 Point-of-Sale (POS)</h3>
                            <p style="font-size:0.9rem; color:#555;">Record over-the-counter sales. Stock will automatically deduct.</p>
                            <input type="number" id="posStuId" placeholder="Buyer Student ID">
                            <div class="grid-2">
                                <input type="number" id="posItemId" placeholder="Item ID">
                                <input type="number" id="posQty" placeholder="Quantity">
                            </div>
                            <button class="btn btn-success" onclick="sendAction('/api/inventory/sell', {student_id: document.getElementById('posStuId').value, item_id: document.getElementById('posItemId').value, quantity: document.getElementById('posQty').value})">Process Sale & Deduct Stock</button>
                        </div>
                    </div>
                </div>

                <!-- 3. EXEAT MODULE -->
                <div id="exeat-section" class="card grid-2 admin-section hidden" style="border-top: 5px solid #e83e8c;">
                    <div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; border: 1px solid #eee;">
                            <h3 style="margin-top:0; color:#e83e8c;">🎫 Issue Digital Exeat</h3>
                            <p style="font-size:0.85rem; color:#666;">Issuing an exeat will immediately trigger a live SMS alert to the student's guardian.</p>
                            <input type="number" id="exStuId" placeholder="Student ID">
                            <select id="exType"><option value="Weekend Exeat">Weekend Exeat</option><option value="Medical Exeat">Medical Emergency</option><option value="Special Permission">Special Permission</option></select>
                            <input type="text" id="exReason" placeholder="Reason for leaving campus">
                            <label style="font-size:0.8rem; font-weight:bold; color:#555;">Expected Return Date</label>
                            <input type="date" id="exReturn">
                            <button class="btn" style="background:#e83e8c;" onclick="sendAction('/api/exeats', {student_id: document.getElementById('exStuId').value, exeat_type: document.getElementById('exType').value, reason: document.getElementById('exReason').value, expected_return: document.getElementById('exReturn').value})">Issue Exeat & Alert Parent</button>
                        </div>
                    </div>
                    <div>
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #ccc; margin-bottom: 20px;">
                            <h3 style="margin-top:0; color:#333;">Mark Student Returned</h3>
                            <input type="number" id="exIdToReturn" placeholder="Exeat ID">
                            <button class="btn btn-success" onclick="sendAction('/api/exeats', {exeat_id: document.getElementById('exIdToReturn').value}, false, 'PUT')">Mark as Safely Returned</button>
                        </div>
                        <button class="btn btn-info" onclick="loadExeats()">View Exeat Ledger</button>
                    </div>
                </div>

                <!-- 4. SICK BAY MODULE -->
                <div id="sickbay-section" class="card grid-2 admin-section hidden" style="border-top: 5px solid #d63384;">
                    <div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; border: 1px solid #eee;">
                            <h3 style="margin-top:0; color:#d63384;">🏥 Log Medical Visit</h3>
                            <input type="number" id="sbStuId" placeholder="Student ID">
                            <input type="text" id="sbSymp" placeholder="Symptoms (e.g. High fever, headache)">
                            <input type="text" id="sbTreat" placeholder="Treatment / Action Taken">
                            <div style="margin-bottom:15px; display:flex; align-items:center; gap:10px;">
                                <input type="checkbox" id="sbAlert" style="width:20px; height:20px; margin:0;">
                                <label style="font-weight:bold; color:var(--danger);">SEND EMERGENCY SMS TO PARENT</label>
                            </div>
                            <button class="btn" style="background:#d63384;" onclick="sendAction('/api/sickbay', {student_id: document.getElementById('sbStuId').value, symptoms: document.getElementById('sbSymp').value, treatment: document.getElementById('sbTreat').value, alert_parent: document.getElementById('sbAlert').checked})">Log Medical Record</button>
                        </div>
                    </div>
                    <div>
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #ccc; height:100%; box-sizing:border-box;">
                            <h3 style="margin-top:0; color:#333;">Infirmary Ledger</h3>
                            <p style="font-size:0.9rem; color:#555;">Maintain an immutable log of all medical treatments administered on campus to prevent liability.</p>
                            <button class="btn btn-info" onclick="loadSickBay()">View Medical History Logs</button>
                        </div>
                    </div>
                </div>

                {% endif %}

                <!-- Shared Teacher/Admin/Guardian Sections -->
                
                <div id="calendar-section" class="card admin-section hidden" style="border-top: 5px solid #20c997;">
                    <h3>📅 Academic Master Calendar</h3>
                    <div class="grid-2">
                        {% if current_user.role == 'admin' %}
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; border: 1px solid #eee;">
                            <h4 style="margin-top:0; color:#20c997;">Add Key Term Event</h4>
                            <input type="text" id="calTitle" placeholder="Event Title (e.g. Mid-Term Break)">
                            <input type="date" id="calDate">
                            <input type="text" id="calDesc" placeholder="Description">
                            <button class="btn" style="background:#20c997;" onclick="sendAction('/api/calendar', {title: document.getElementById('calTitle').value, date: document.getElementById('calDate').value, desc: document.getElementById('calDesc').value})">Publish to Master Calendar</button>
                        </div>
                        {% endif %}
                        <div style="background: #e9ecef; padding: 20px; border-radius: 12px; border: 1px solid #ccc; {% if current_user.role != 'admin' %}grid-column: span 2;{% endif %}">
                            <h4 style="margin-top:0; color:#333;">Upcoming Term Events</h4>
                            <button class="btn btn-info" onclick="loadCalendar()">View Full Calendar</button>
                        </div>
                    </div>
                </div>

                {% if current_user.role in ['admin', 'teacher'] %}
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
                {% endif %}

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
                            <button class="btn btn-info" onclick="loadStaff()">View Official Staff Directory</button>
                        </div>
                    </div>
                </div>
                {% endif %}
            {% endif %}

            <!-- UNIVERSAL SETTINGS & SECURITY TAB -->
            {% if current_user.is_authenticated and current_user.role != 'superadmin' %}
            <div id="settings-section" class="card admin-section hidden" style="border-top: 5px solid #333;">
                <h3>🔒 Security & Access Management (SECURED)</h3>
                <div class="grid-2">
                    <div style="background: #fff; padding: 25px; border-radius: 12px; border: 1px solid #ccc; box-shadow: 0 4px 10px rgba(0,0,0,0.05);">
                        <h4 style="margin-top:0; color: #333;">Change Personal Password</h4>
                        <input type="password" id="myOldPass" placeholder="Current Password">
                        <input type="password" id="myNewPass" placeholder="New Secure Password">
                        <button class="btn" style="background:#333;" onclick="changeMyPassword()">Update My Password</button>
                    </div>

                    {% if current_user.role == 'admin' %}
                    <div style="background: #f8d7da; padding: 25px; border-radius: 12px; border: 1px solid var(--danger);">
                        <h4 style="margin-top:0; color: #721c24;">Master Credential Override</h4>
                        <input type="email" id="resetTargetEmail" placeholder="Target User Email (e.g. teacher@school.com)">
                        <input type="password" id="resetNewPass" placeholder="Assign New Temporary Password">
                        <button class="btn btn-danger" onclick="adminResetPassword()">Execute Force Reset</button>
                    </div>
                    {% endif %}
                </div>

                {% if current_user.role == 'admin' %}
                <div style="background: #fff; padding: 25px; border-radius: 12px; border: 1px solid #ccc; box-shadow: 0 4px 10px rgba(0,0,0,0.05); margin-top: 20px;">
                    <h4 style="margin-top:0; color: #333;">📜 Security Audit Trail</h4>
                    <button class="btn btn-primary" onclick="loadAuditLogs()">View Master Audit Logs</button>
                </div>

                <h3 style="margin-top:40px;">⚙️ Institution Profile Settings</h3>
                <div style="background: #f8f9fa; padding: 25px; border-radius: 12px; border: 1px solid #eee; max-width: 600px;">
                    <label style="font-weight:bold; color:var(--primary); margin-bottom:8px; display:block;">Official School Address / Location</label>
                    <input type="text" id="setAddress" placeholder="e.g., P.O Box 123, Winneba" style="font-size:1.05rem;">
                    <label style="font-weight:bold; color:var(--primary); margin-bottom:8px; display:block; margin-top:15px;">Official Contact Number</label>
                    <input type="text" id="setPhone" placeholder="e.g., 0244123456" style="font-size:1.05rem;">
                    <button class="btn btn-success" style="margin-top: 15px; font-size:1.05rem;" onclick="sendAction('/api/settings', {address: document.getElementById('setAddress').value, phone: document.getElementById('setPhone').value})">Save Profile Updates</button>
                </div>
                {% endif %}
            </div>
            {% endif %}

            <!-- Shared Data Viewer -->
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
            document.onmousemove = resetTimer; document.onkeypress = resetTimer;

            function showSection(sectionId) {
                const sections = ['analytics-section', 'finance-section', 'admissions-section', 'attendance-section', 'academics-section', 'sms-section', 'hr-section', 'guardian-section', 'settings-section', 'sa-settings-section', 'transport-section', 'store-section', 'exeat-section', 'sickbay-section', 'calendar-section'];
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

            async function secureSection(sectionId) {
                if (unlockedSections[sectionId]) { showSection(sectionId); return; }
                const pass = prompt("SECURE VAULT: Please enter your personal password to access this restricted section.");
                if (!pass) return;
                try {
                    const res = await fetch('/api/verify_password', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: pass}) });
                    if(res.ok) { unlockedSections[sectionId] = true; showSection(sectionId); showToast("Vault Unlocked Successfully"); } 
                    else { const data = await res.json(); showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            function showToast(message, isError=false) {
                const toast = document.getElementById('toast');
                toast.innerText = message; toast.style.background = isError ? '#dc3545' : '#28a745';
                toast.style.display = 'block'; setTimeout(() => { toast.style.display = 'none'; }, 5000);
            }

            function downloadCSV(csv, filename) {
                let csvFile = new Blob([csv], {type: "text/csv"}); let downloadLink = document.createElement("a");
                downloadLink.download = filename; downloadLink.href = window.URL.createObjectURL(csvFile);
                downloadLink.style.display = "none"; document.body.appendChild(downloadLink); downloadLink.click();
            }

            function exportTableToCSV(filename) {
                let csv = []; let rows = document.querySelectorAll("#table-container table tr");
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
                const res = await fetch('/api/login', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: emailInput, password: document.getElementById('pass').value}) });
                const data = await res.json();
                if (res.ok) { showToast(data.message); setTimeout(() => window.location.reload(), 1000); } 
                else { showToast(data.error, true); }
            }

            async function logout() { await fetch('/api/logout', { method: 'POST' }); window.location.reload(); }

            async function sendAction(endpoint, payload, isGet=false, methodType='POST') {
                try {
                    const options = isGet ? {} : { method: methodType, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) };
                    const res = await fetch(endpoint, options);
                    const data = await res.json();
                    if (res.ok) { 
                        showToast(data.message || "Success"); 
                        if (endpoint === '/api/setup_db') loadSchools(); 
                        if (endpoint === '/api/expenses') loadDashboardData();
                        if (endpoint === '/api/transport') loadTransport();
                        if (endpoint === '/api/inventory' || endpoint === '/api/inventory/sell') loadInventory();
                        if (endpoint === '/api/exeats') loadExeats();
                        if (endpoint === '/api/sickbay') loadSickBay();
                        if (endpoint === '/api/calendar') loadCalendar();
                    }
                    else if (res.status === 402) showToast(data.error, true); 
                    else showToast(data.error || "Error", true);
                } catch(e) { showToast("Connection failed", true); }
            }

            // --- CREDENTIAL MANAGEMENT ---
            async function updateSACredentials() {
                const email = document.getElementById('saNewEmail').value; const pass = document.getElementById('saNewPass').value;
                if(!email && !pass) { showToast("Enter a new email or password.", true); return; }
                if(!confirm("Are you sure you want to update the master credentials?")) return;
                try {
                    const res = await fetch('/api/superadmin/credentials', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({new_email: email, new_password: pass}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('saNewEmail').value = ''; document.getElementById('saNewPass').value = ''; } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function changeMyPassword() {
                const oldP = document.getElementById('myOldPass').value; const newP = document.getElementById('myNewPass').value;
                if(!oldP || !newP) { showToast("Provide both passwords.", true); return; }
                try {
                    const res = await fetch('/api/change_password', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({current_password: oldP, new_password: newP}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('myOldPass').value = ''; document.getElementById('myNewPass').value = ''; } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function adminResetPassword() {
                const email = document.getElementById('resetTargetEmail').value; const newP = document.getElementById('resetNewPass').value;
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
                const email = document.getElementById('saResetEmail').value; const newP = document.getElementById('saResetPass').value;
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
                            const img = new Image(); img.onload = function() {
                                const canvas = document.createElement('canvas');
                                const MAX_WIDTH = 300; const scaleSize = MAX_WIDTH / img.width;
                                canvas.width = MAX_WIDTH; canvas.height = img.height * scaleSize;
                                const ctx = canvas.getContext('2d'); ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                                resolve(canvas.toDataURL('image/jpeg', 0.7)); 
                            }; img.src = e.target.result;
                        }; reader.readAsDataURL(file);
                    });
                }
                const payload = {
                    first_name: document.getElementById('sFirst').value, last_name: document.getElementById('sLast').value,
                    current_class: document.getElementById('sClass').value, transport_route: document.getElementById('sRoute').value,
                    guardian_name: document.getElementById('sGName').value, guardian_contact: document.getElementById('sGContact').value,
                    boarding_status: document.getElementById('sBoarding').value, house: document.getElementById('sHouse').value, photo_b64: photo_b64
                };
                sendAction('/api/students', payload);
            }

            async function editStudent() {
                const id = document.getElementById('editStuId').value;
                if(!id) { showToast("Enter Target Student ID", true); return; }
                const payload = {
                    first_name: document.getElementById('editFirst').value, last_name: document.getElementById('editLast').value,
                    current_class: document.getElementById('editClass').value, boarding_status: document.getElementById('editBoarding').value,
                    guardian_contact: prompt("Update Guardian Phone Number:"), house: prompt("Update House Assignment:"), transport_route: prompt("Update Transport Route (or type 'None'):")
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
                const formData = new FormData(); formData.append('file', fileInput.files[0]); showToast("Uploading roster, please wait...", false);
                try {
                    const res = await fetch('/api/students/bulk', { method: 'POST', body: formData });
                    const data = await res.json();
                    if (res.ok) { showToast(data.message); loadRoster(); } else showToast(data.error, true);
                } catch(e) { showToast("Upload failed", true); }
                fileInput.value = ''; 
            }

            async function bulkBillClass() {
                const payload = {
                    target_class: document.getElementById('bbClass').value, fee_category: document.getElementById('bbCat').value,
                    amount_due: document.getElementById('bbAmount').value, academic_year: document.getElementById('bbYear').value,
                    term: document.getElementById('bbTerm').value, description: document.getElementById('bbDesc').value
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
                    if (res.ok) { showToast(data.message); loadRoster(); } else showToast(data.error, true);
                } catch(e) { showToast("Connection failed", true); }
            }

            async function deleteBill(feeId, stuId) {
                if(!confirm("Are you sure you want to completely REVERSE this bill and delete its record?")) return;
                try {
                    const res = await fetch('/api/fees/' + feeId, { method: 'DELETE' });
                    const data = await res.json();
                    if (res.ok) { showToast(data.message); loadStatement(stuId); } else showToast(data.error, true);
                } catch(e) { showToast("Connection failed", true); }
            }

            async function promoteClass() {
                const fromC = document.getElementById('promoFrom').value; const toC = document.getElementById('promoTo').value;
                if(!fromC || !toC) { showToast("Enter both classes", true); return; }
                if(!confirm("Are you sure you want to promote ALL students currently in " + fromC + " to " + toC + "?")) return;
                try {
                    const res = await fetch('/api/students/promote', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({from_class: fromC, to_class: toC}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); loadRoster(); } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function registerStaff() {
                const payload = {
                    email: document.getElementById('tEmail').value, password: document.getElementById('tPass').value,
                    phone: document.getElementById('tPhone').value, subject: document.getElementById('tSubj').value, salary: document.getElementById('tSal').value
                };
                try {
                    const res = await fetch('/api/register_staff', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('tEmail').value = ''; document.getElementById('tPass').value = ''; document.getElementById('tPhone').value = ''; document.getElementById('tSubj').value = ''; document.getElementById('tSal').value = ''; loadStaff(); } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function registerGuardian() {
                const payload = { email: document.getElementById('gEmail').value, password: document.getElementById('gPass').value, linked_student_id: document.getElementById('gStuId').value };
                try {
                    const res = await fetch('/api/register_guardian', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('gEmail').value = ''; document.getElementById('gPass').value = ''; document.getElementById('gStuId').value = ''; } 
                    else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function issueBill() {
                const stuId = document.getElementById('bStuId').value;
                const payload = {
                    student_id: stuId, fee_category: document.getElementById('bCat').value, amount_due: document.getElementById('bAmount').value,
                    academic_year: document.getElementById('bYear').value, term: document.getElementById('bTerm').value, description: document.getElementById('bDesc').value
                };
                try {
                    const res = await fetch('/api/fees/bill', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); document.getElementById('bAmount').value = ''; document.getElementById('bDesc').value = ''; loadStatement(stuId); } 
                    else { showToast(data.error, true); }
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
                    if(res.ok) { showToast(data.message); loadStatement(stuId); } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            function printReportCard() {
                const id = document.getElementById('repId').value;
                if (!id) { showToast("Please enter a Student ID", true); return; }
                window.open('/print_report/' + id, '_blank');
            }

            // --- SUPERADMIN SPECIFIC FUNCTIONS ---
            function openBrandingModal(schoolId, currentColor, schoolName) {
                document.getElementById('branding-div').classList.remove('hidden'); document.getElementById('brandSchoolId').value = schoolId;
                document.getElementById('brandColor').value = currentColor || '#0f4c81'; document.getElementById('brandSchoolName').innerText = schoolName;
                document.getElementById('branding-div').scrollIntoView({behavior: "smooth"});
            }

            async function saveBranding() {
                const id = document.getElementById('brandSchoolId').value; const color = document.getElementById('brandColor').value;
                const fileInput = document.getElementById('brandLogo'); let logo_b64 = null;
                if (fileInput.files.length > 0) {
                    const file = fileInput.files[0];
                    logo_b64 = await new Promise((resolve) => { const reader = new FileReader(); reader.onload = function(e) { resolve(e.target.result); }; reader.readAsDataURL(file); });
                }
                try {
                    const res = await fetch('/api/superadmin/branding/' + id, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({color: color, logo_b64: logo_b64}) });
                    const data = await res.json();
                    if(res.ok) { showToast(data.message); loadSchools(); document.getElementById('branding-div').classList.add('hidden'); } else { showToast(data.error, true); }
                } catch(e) { showToast("Connection failed", true); }
            }

            async function loadSchools() {
                const res = await fetch('/api/superadmin/schools'); if(!res.ok) return;
                const data = await res.json();
                let html = '<table style="width:100%; border-collapse: collapse; text-align: left;"><tr><th style="padding:15px; border-bottom:2px solid #ddd;">ID</th><th style="padding:15px; border-bottom:2px solid #ddd;">School Name</th><th style="padding:15px; border-bottom:2px solid #ddd;">Expiry Date</th><th style="padding:15px; border-bottom:2px solid #ddd;">Status</th><th style="padding:15px; border-bottom:2px solid #ddd;">Actions</th></tr>';
                data.data.forEach(s => {
                    const statusColor = s.status === 'Active' ? 'green' : 'red';
                    html += `<tr><td style="padding:15px; border-bottom:1px solid #eee; font-weight:bold;">${s.school_id}</td>
                        <td style="padding:15px; border-bottom:1px solid #eee;"><div style="display:flex; align-items:center; gap:10px;"><div style="width:15px; height:15px; border-radius:50%; background:${s.primary_color};"></div><strong>${s.school_name}</strong></div></td>
                        <td style="padding:15px; border-bottom:1px solid #eee;">${s.expiry_date}</td><td style="color:${statusColor}; font-weight:bold; padding:15px; border-bottom:1px solid #eee;">${s.status}</td>
                        <td style="padding:15px; border-bottom:1px solid #eee;">
                            <button class="btn btn-warning" style="width: auto; padding: 6px 12px; margin: 2px; color:#333;" onclick="openBrandingModal(${s.school_id}, '${s.primary_color}', '${s.school_name}')">🎨 Brand</button>
                            <button class="btn btn-success" style="width: auto; padding: 6px 12px; margin: 2px;" onclick="sendAction('/api/superadmin/renew/${s.school_id}', {})">Renew</button>
                            <button class="btn btn-info" style="width: auto; padding: 6px 12px; margin: 2px;" onclick="window.location.href='/api/superadmin/backup/${s.school_id}'">⬇️ Backup</button>
                            <input type="file" id="file_${s.school_id}" accept=".json" style="display:none;" onchange="uploadRestore(${s.school_id})">
                            <button class="btn btn-danger" style="width: auto; padding: 6px 12px; margin: 2px;" onclick="document.getElementById('file_${s.school_id}').click()">⬆️ Restore</button>
                        </td></tr>`;
                });
                document.getElementById('school-container').innerHTML = html + '</table>';
            }

            async function onboardNewSchool() {
                const res = await fetch('/api/superadmin/onboard', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({school_name: document.getElementById('onboardSchool').value, admin_email: document.getElementById('onboardEmail').value, admin_password: document.getElementById('onboardPass').value}) });
                const data = await res.json(); if (res.ok) { showToast(data.message); loadSchools(); } else { showToast(data.error, true); }
            }

            async function uploadRestore(schoolId) {
                const fileInput = document.getElementById('file_' + schoolId); if (!fileInput.files.length) return;
                const formData = new FormData(); formData.append('file', fileInput.files[0]); showToast("Restoring data, please wait...", false);
                try {
                    const res = await fetch('/api/superadmin/restore/' + schoolId, { method: 'POST', body: formData });
                    const data = await res.json();
                    if (res.ok) showToast(data.message); else showToast(data.error, true);
                } catch(e) { showToast("Upload failed", true); }
                fileInput.value = ''; 
            }

            // --- DATA RENDERERS ---
            function renderTable(title, headers, rows, keys) {
                const viewer = document.getElementById('data-viewer'); document.getElementById('viewer-title').innerText = title; const container = document.getElementById('table-container');
                if (!rows || rows.length === 0) { container.innerHTML = '<div style="padding: 20px; color:#666;">No records found in database.</div>'; viewer.classList.remove('hidden'); return; }
                let html = '<table style="width:100%; border-collapse: collapse;"><tr>';
                headers.forEach(h => html += `<th style="background:#f8f9fa; color:var(--primary); padding:15px; text-align:left; border-bottom: 2px solid #ddd; font-weight:bold;">${h}</th>`); html += '</tr>';
                rows.forEach(row => {
                    html += '<tr class="table-row">';
                    keys.forEach(k => {
                        let val = row[k];
                        if (k === 'photo') { html += `<td style="padding:15px; border-bottom:1px solid #eee; width: 60px;"><img src="/api/photo/${row['student_id']}" style="width:45px; height:45px; border-radius:8px; object-fit:cover; border:2px solid #ccc; background:#eee;"></td>`; } 
                        else if (k === 'action_pay') {
                            html += `<td style="padding:15px; border-bottom:1px solid #eee;">`;
                            if (row['remaining_balance'] > 0) { html += `<button class="btn btn-success" style="padding:6px 12px; font-size:0.85rem; margin:0; width:auto;" onclick="processPayment(${row['fee_id']}, ${row['remaining_balance']}, ${row['student_id']})">Pay Bill</button>`; } 
                            else { html += `<span style="color:var(--accent); font-weight:bold; margin-right: 15px;">Cleared</span>`; }
                            html += `<button class="btn btn-danger" style="padding:6px 12px; font-size:0.85rem; margin:0 0 0 5px; width:auto; background:white; color:var(--danger); border:1px solid var(--danger); box-shadow:none;" onclick="deleteBill(${row['fee_id']}, ${row['student_id']})">Reverse Bill</button></td>`;
                        } 
                        else if (k === 'action_roster') { html += `<td style="padding:15px; border-bottom:1px solid #eee;"><button class="btn btn-danger" style="padding:6px 12px; font-size:0.85rem; margin:0; width:auto;" onclick="deleteStudent(${row['student_id']})">🗑️ Delete</button></td>`; } 
                        else if (k === 'action_debtor') { html += `<td style="padding:15px; border-bottom:1px solid #eee;"><button class="btn btn-info" style="padding:6px 12px; font-size:0.85rem; margin:0; width:auto;" onclick="loadStatement(${row['student_id']})">Open Ledger</button></td>`; } 
                        else if (k === 'action_return_exeat') { 
                            if(row['status'] === 'Active') { html += `<td style="padding:15px; border-bottom:1px solid #eee;"><button class="btn btn-success" style="padding:6px 12px; font-size:0.85rem; margin:0; width:auto;" onclick="sendAction('/api/exeats', {exeat_id: ${row['exeat_id']}}, false, 'PUT')">Mark Returned</button></td>`; }
                            else { html += `<td style="padding:15px; border-bottom:1px solid #eee; font-weight:bold; color:var(--primary);">Returned</td>`; }
                        }
                        else if (k === 'remaining_balance' || k === 'arrears') { let color = val > 0 ? '#dc3545' : '#28a745'; html += `<td style="color:${color}; font-weight:bold; padding:15px; border-bottom:1px solid #eee;">${val}</td>`; } 
                        else if (k === 'boarding_status') { let badge = val === 'Boarding' ? 'background:var(--primary);color:white;' : 'background:#e9ecef;color:#555;'; html += `<td style="padding:15px; border-bottom:1px solid #eee;"><span style="${badge}padding:4px 10px;border-radius:12px;font-size:0.8rem; font-weight:bold;">${val}</span></td>`; } 
                        else { html += `<td style="padding:15px; border-bottom:1px solid #eee;">${val}</td>`; }
                    }); html += '</tr>';
                }); container.innerHTML = html + '</table>'; viewer.classList.remove('hidden'); viewer.scrollIntoView({behavior: "smooth"});
            }

            async function loadRoster() { const res = await fetch('/api/students'); const data = await res.json(); renderTable("Student Directory", ['Photo', 'ID', 'First Name', 'Last Name', 'Class', 'Bus Route', 'Status', 'House', 'Guardian Contact', 'Action'], data.data, ['photo', 'student_id', 'first_name', 'last_name', 'current_class', 'transport_route', 'boarding_status', 'house', 'guardian_contact', 'action_roster']); }
            async function loadStaff() { const res = await fetch('/api/staff'); const data = await res.json(); renderTable("Staff Directory", ['Teacher Email', 'Phone Number', 'Subject', 'Base Salary (GHS)'], data.data, ['email', 'phone', 'subject', 'base_salary']); }
            async function loadExpenses() { const res = await fetch('/api/expenses'); const data = await res.json(); renderTable("Expense Ledger", ['Date', 'Category', 'Description', 'Amount (GHS)'], data.data, ['date', 'category', 'description', 'amount']); }
            async function loadDebtors() { const res = await fetch('/api/debtors'); const data = await res.json(); renderTable("Arrears & Debtors Tracker", ['ID', 'First Name', 'Last Name', 'Parent Contact', 'Total Owed (GHS)', 'Action'], data.data, ['student_id', 'first_name', 'last_name', 'guardian_contact', 'arrears', 'action_debtor']); }
            async function loadAuditLogs() { const res = await fetch('/api/audit_logs'); const data = await res.json(); renderTable("Master Security Audit Trail", ['Timestamp', 'System User (Email)', 'Executed Action', 'Target Record / Details'], data.data, ['time', 'user_email', 'action', 'target']); }
            async function loadTransport() { const res = await fetch('/api/transport'); const data = await res.json(); renderTable("Bus Routes Ledger", ['Route ID', 'Route Name', 'Driver', 'Term Fare (GHS)'], data.data, ['route_id', 'route_name', 'driver_name', 'fare']); }
            async function loadInventory() { const res = await fetch('/api/inventory'); const data = await res.json(); renderTable("Store Inventory Catalog", ['Item ID', 'Item Name', 'Price (GHS)', 'Current Stock Quantity'], data.data, ['item_id', 'item_name', 'price', 'stock']); }
            async function loadExeats() { const res = await fetch('/api/exeats'); const data = await res.json(); renderTable("Active Boarding Exeats", ['Exeat ID', 'Student ID', 'First Name', 'Last Name', 'Type', 'Reason', 'Expected Return Date', 'Status'], data.data, ['exeat_id', 'student_id', 'first_name', 'last_name', 'exeat_type', 'reason', 'date', 'action_return_exeat']); }
            async function loadSickBay() { const res = await fetch('/api/sickbay'); const data = await res.json(); renderTable("Infirmary Medical Logs", ['Log ID', 'Time/Date', 'Student ID', 'First Name', 'Last Name', 'Symptoms', 'Treatment Administered'], data.data, ['log_id', 'time', 'student_id', 'first_name', 'last_name', 'symptoms', 'treatment']); }
            async function loadCalendar() { const res = await fetch('/api/calendar'); const data = await res.json(); renderTable("Academic Term Calendar", ['Event Date', 'Event Title', 'Description'], data.data, ['date', 'event_title', 'description']); }
            
            async function loadReport() {
                const id = document.getElementById('repId').value; if(!id) { showToast("Please enter a Student ID", true); return; }
                const res = await fetch('/api/report_card/' + id); if (!res.ok) { showToast("Not Found", true); return; }
                const data = await res.json(); renderTable("Raw Grading Data", ['Subject', 'Class', 'Exam', 'Total', 'Grade', 'Remarks', 'Term'], data.grades, ['subject_name', 'class_score', 'exam_score', 'total_score', 'waec_grade', 'teacher_remarks', 'term']);
            }
            
            async function loadStatement(overrideId) {
                const id = overrideId || (document.getElementById('stateStuId') ? document.getElementById('stateStuId').value : null);
                if(!id) { showToast("Please enter a Student ID", true); return; }
                const res = await fetch('/api/statement/' + id); if (!res.ok) { showToast("Ledger Not Found", true); return; }
                const data = await res.json();
                if(document.getElementById('stateStuId')) { renderTable("Financial Ledger (Student ID: " + id + ")", ['Bill Type', 'Year', 'Term', 'Desc', 'Due', 'Paid', 'Balance', 'Action'], data.statement, ['fee_category', 'academic_year', 'term', 'description', 'amount_due', 'total_paid', 'remaining_balance', 'action_pay']); } 
                else { renderTable("Financial Ledger (Student ID: " + id + ")", ['Bill Type', 'Year', 'Term', 'Desc', 'Due', 'Paid', 'Balance'], data.statement, ['fee_category', 'academic_year', 'term', 'description', 'amount_due', 'total_paid', 'remaining_balance']); }
            }

            let financeChartInstance = null; let waecChartInstance = null;
            async function loadDashboardData() {
                if (document.getElementById('attDate')) { document.getElementById('attDate').value = new Date().toISOString().split('T')[0]; }
                if (document.getElementById('school-container')) { loadSchools(); }
                if (document.getElementById('financeChart')) {
                    try {
                        const res = await fetch('/api/analytics'); const data = await res.json();
                        document.getElementById('metricRevenue').innerText = `Gross Revenue: GHS ${data.financials.paid.toLocaleString()}`;
                        document.getElementById('metricExpenses').innerText = `Total Expenses: GHS ${data.financials.expenses.toLocaleString()}`;
                        document.getElementById('metricMargin').innerText = `Net Margin: GHS ${data.financials.net_margin.toLocaleString()}`;
                        
                        if (financeChartInstance) financeChartInstance.destroy();
                        financeChartInstance = new Chart(document.getElementById('financeChart').getContext('2d'), { type: 'doughnut', data: { labels: ['Revenue', 'Arrears', 'Expenses'], datasets: [{ data: [data.financials.paid, data.financials.outstanding, data.financials.expenses], backgroundColor: ['#28a745', '#ffc107', '#dc3545'], borderWidth: 0 }] }, options: { responsive: true, maintainAspectRatio: false } });
                        const labels = data.performance.map(p => p.waec_grade); const counts = data.performance.map(p => p.count);
                        if (waecChartInstance) waecChartInstance.destroy();
                        waecChartInstance = new Chart(document.getElementById('waecChart').getContext('2d'), { type: 'bar', data: { labels: labels, datasets: [{ label: 'Students', data: counts, backgroundColor: '{{ primary_color }}' }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { title: { display: true, text: 'WAEC Grade Distribution' } } } });
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
