import os
import psycopg2
import csv
import json
import decimal
import boto3
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

# --- HELPER FUNCTIONS ---
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

# --- 1. UNIFIED AUTH MODEL ---
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

# --- 2. THE SAAS BOUNCER ---
def require_active_subscription(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.role == 'superadmin':
            return f(*args, **kwargs)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT subscription_expiry_date FROM institutions WHERE school_id = %s", (current_user.school_id,))
        school = cur.fetchone()
        cur.close(); conn.close()
        if not school or school['subscription_expiry_date'] < datetime.now().date():
            return jsonify({"error": "ACCESS LOCKED: Your annual subscription has expired. Please remit payment to restore your database access."}), 402 
        return f(*args, **kwargs)
    return decorated_function

# --- 3. SYSTEM SETUP ---
@app.route('/api/setup_db')
def setup_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS institutions (school_id SERIAL PRIMARY KEY, school_name VARCHAR(150) NOT NULL UNIQUE, subscription_expiry_date DATE NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS students (student_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, first_name VARCHAR(100) NOT NULL, last_name VARCHAR(100) NOT NULL, guardian_name VARCHAR(100) NOT NULL, guardian_contact VARCHAR(20) NOT NULL, enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    
    # Safe Alteration: Inject Day/Boarding & House into existing Students table without data loss
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS boarding_status VARCHAR(20) DEFAULT 'Day'")
    cur.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS house VARCHAR(100) DEFAULT 'Unassigned'")

    cur.execute("CREATE TABLE IF NOT EXISTS system_users (user_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, email VARCHAR(100) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, linked_student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE)")
    cur.execute("CREATE TABLE IF NOT EXISTS subjects (subject_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100) NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS grades (grade_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, subject_id INTEGER REFERENCES subjects(subject_id) ON DELETE CASCADE, class_score INTEGER NOT NULL, exam_score INTEGER NOT NULL, total_score INTEGER NOT NULL, waec_grade VARCHAR(2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL, teacher_remarks VARCHAR(255))")
    cur.execute("CREATE TABLE IF NOT EXISTS fees (fee_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, fee_category VARCHAR(50) NOT NULL, description VARCHAR(255) NOT NULL, amount_due DECIMAL(10, 2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL, date_issued TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS payments (payment_id SERIAL PRIMARY KEY, fee_id INTEGER REFERENCES fees(fee_id) ON DELETE CASCADE, amount_paid DECIMAL(10, 2) NOT NULL, payment_method VARCHAR(50) NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    
    cur.execute("SELECT * FROM system_users WHERE role = 'superadmin'")
    if not cur.fetchone():
        hashed_sa = generate_password_hash('ceo123')
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s)", ('superadmin@engine.com', hashed_sa, 'superadmin'))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": "Multi-Tenant SaaS Engine Ready! Boarding/House parameters successfully injected."})

# --- 4. THE AUTOMATED CLOUD BACKUP ROBOT ---
def automated_weekly_backup():
    if not AWS_BUCKET_NAME: return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT school_id, school_name FROM institutions")
    schools = cur.fetchall()
    
    for school in schools:
        school_id = school['school_id']
        school_name = school['school_name']
        backup = {"school_name": school_name, "export_date": datetime.now().isoformat(), "data": {}}
        
        cur.execute("SELECT * FROM subjects WHERE school_id = %s", (school_id,))
        backup['data']['subjects'] = cur.fetchall()
        cur.execute("SELECT * FROM students WHERE school_id = %s", (school_id,))
        backup['data']['students'] = cur.fetchall()
        cur.execute("SELECT * FROM fees WHERE school_id = %s", (school_id,))
        backup['data']['fees'] = cur.fetchall()
        cur.execute("SELECT p.* FROM payments p JOIN fees f ON p.fee_id = f.fee_id WHERE f.school_id = %s", (school_id,))
        backup['data']['payments'] = cur.fetchall()
        cur.execute("SELECT * FROM grades WHERE school_id = %s", (school_id,))
        backup['data']['grades'] = cur.fetchall()
        
        json_data = json.dumps(backup, default=custom_json_serializer)
        filename = f"Automated_Backups/{school_name.replace(' ', '_')}/Backup_{datetime.now().strftime('%Y%m%d')}.json"
        
        try:
            s3_client.put_object(Bucket=AWS_BUCKET_NAME, Key=filename, Body=json_data)
        except Exception as e: pass
            
    cur.close(); conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=automated_weekly_backup, trigger="cron", day_of_week='sun', hour=23, minute=59)
scheduler.start()

# --- 5. AUTHENTICATION ---
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, email, password_hash, role, linked_student_id, school_id FROM system_users WHERE email = %s", (data.get('email'),))
    user_data = cur.fetchone()
    cur.close(); conn.close()
    if user_data and check_password_hash(user_data['password_hash'], data.get('password')):
        user = User(user_data['user_id'], user_data['email'], user_data['role'], user_data['linked_student_id'], user_data['school_id'])
        login_user(user)
        return jsonify({"message": f"Logged in as {user.role}."})
    return jsonify({"error": "Invalid credentials!"}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Logged out safely."})

# --- 6. SUPER ADMIN & MANUAL VAULT ENDPOINTS ---
@app.route('/api/superadmin/schools', methods=['GET'])
@login_required
def get_schools():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT school_id, school_name, TO_CHAR(subscription_expiry_date, 'YYYY-MM-DD') as expiry_date, CASE WHEN subscription_expiry_date >= CURRENT_DATE THEN 'Active' ELSE 'Expired' END as status FROM institutions ORDER BY school_id")
    schools = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"data": schools})

@app.route('/api/superadmin/renew/<int:school_id>', methods=['POST'])
@login_required
def renew_school(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE institutions SET subscription_expiry_date = CURRENT_DATE + INTERVAL '1 year' WHERE school_id = %s RETURNING school_name, subscription_expiry_date", (school_id,))
    updated = cur.fetchone()
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Contract Renewed! {updated['school_name']} active until {updated['subscription_expiry_date']}"})

@app.route('/api/superadmin/onboard', methods=['POST'])
@login_required
def onboard_school():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO institutions (school_name, subscription_expiry_date) VALUES (%s, CURRENT_DATE + INTERVAL '1 year') RETURNING school_id", (data.get('school_name'),))
        new_school_id = cur.fetchone()['school_id']
        hashed = generate_password_hash(data.get('admin_password'))
        cur.execute("INSERT INTO system_users (school_id, email, password_hash, role) VALUES (%s, %s, %s, 'admin')", (new_school_id, data.get('admin_email'), hashed))
        conn.commit()
        return jsonify({"message": f"Onboarded {data.get('school_name')}!"}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "School name or Admin email already exists!"}), 409
    finally:
        cur.close(); conn.close()

@app.route('/api/superadmin/backup/<int:school_id>', methods=['GET'])
@login_required
def download_backup(school_id):
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT school_name FROM institutions WHERE school_id = %s", (school_id,))
    school = cur.fetchone()
    if not school: return jsonify({"error": "School not found"}), 404
    
    backup = {"school_name": school['school_name'], "export_date": datetime.now().isoformat(), "data": {}}
    
    cur.execute("SELECT * FROM subjects WHERE school_id = %s", (school_id,))
    backup['data']['subjects'] = cur.fetchall()
    cur.execute("SELECT * FROM students WHERE school_id = %s", (school_id,))
    backup['data']['students'] = cur.fetchall()
    cur.execute("SELECT * FROM fees WHERE school_id = %s", (school_id,))
    backup['data']['fees'] = cur.fetchall()
    cur.execute("SELECT p.* FROM payments p JOIN fees f ON p.fee_id = f.fee_id WHERE f.school_id = %s", (school_id,))
    backup['data']['payments'] = cur.fetchall()
    cur.execute("SELECT * FROM grades WHERE school_id = %s", (school_id,))
    backup['data']['grades'] = cur.fetchall()
    
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
        conn = get_db_connection()
        cur = conn.cursor()

        if 'subjects' in data['data']:
            for r in data['data']['subjects']:
                cur.execute("INSERT INTO subjects (subject_id, school_id, subject_name) VALUES (%s, %s, %s) ON CONFLICT (subject_id) DO NOTHING", (r['subject_id'], school_id, r['subject_name']))
        if 'students' in data['data']:
            for r in data['data']['students']:
                b_stat = r.get('boarding_status', 'Day')
                hs = r.get('house', 'Unassigned')
                cur.execute("INSERT INTO students (student_id, school_id, first_name, last_name, guardian_name, guardian_contact, boarding_status, house, enrollment_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (student_id) DO NOTHING", (r['student_id'], school_id, r['first_name'], r['last_name'], r['guardian_name'], r['guardian_contact'], b_stat, hs, r['enrollment_date']))
        if 'fees' in data['data']:
            for r in data['data']['fees']:
                cat = r.get('fee_category', 'General')
                yr = r.get('academic_year', 'Unknown')
                tm = r.get('term', 'Unknown')
                cur.execute("INSERT INTO fees (fee_id, school_id, student_id, fee_category, description, amount_due, academic_year, term, date_issued) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (fee_id) DO NOTHING", (r['fee_id'], school_id, r['student_id'], cat, r['description'], r['amount_due'], yr, tm, r['date_issued']))
        if 'payments' in data['data']:
            for r in data['data']['payments']:
                cur.execute("INSERT INTO payments (payment_id, fee_id, amount_paid, payment_method, payment_date) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (payment_id) DO NOTHING", (r['payment_id'], r['fee_id'], r['amount_paid'], r['payment_method'], r['payment_date']))
        if 'grades' in data['data']:
            for r in data['data']['grades']:
                c_score = r.get('class_score', 0)
                e_score = r.get('exam_score', r.get('score', 0))
                t_score = r.get('total_score', r.get('score', 0))
                rem = r.get('teacher_remarks', '')
                cur.execute("INSERT INTO grades (grade_id, school_id, student_id, subject_id, class_score, exam_score, total_score, waec_grade, academic_year, term, teacher_remarks) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (grade_id) DO NOTHING", (r['grade_id'], school_id, r['student_id'], r['subject_id'], c_score, e_score, t_score, r['waec_grade'], r['academic_year'], r['term'], rem))

        conn.commit()
        cur.close(); conn.close()
        return jsonify({"message": f"Vault Restoration Complete for {data.get('school_name')}!"}), 200
    except Exception as e:
        return jsonify({"error": f"Restoration failed: {str(e)}"}), 500

# --- 7. TENANT (SCHOOL) ENDPOINTS ---
@app.route('/api/analytics', methods=['GET'])
@login_required
@require_active_subscription
def get_analytics():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(amount_due), 0) as total_due FROM fees WHERE school_id = %s", (current_user.school_id,))
    total_due = cur.fetchone()['total_due']
    cur.execute("SELECT COALESCE(SUM(p.amount_paid), 0) as total_paid FROM payments p JOIN fees f ON p.fee_id = f.fee_id WHERE f.school_id = %s", (current_user.school_id,))
    total_paid = cur.fetchone()['total_paid']
    cur.close(); conn.close()
    return jsonify({"financials": {"due": float(total_due), "paid": float(total_paid), "outstanding": float(total_due - total_paid)}})

@app.route('/api/students', methods=['GET', 'POST'])
@login_required
@require_active_subscription
def manage_students():
    conn = get_db_connection()
    cur = conn.cursor()
    if request.method == 'POST':
        data = request.get_json()
        cur.execute("INSERT INTO students (school_id, first_name, last_name, guardian_name, guardian_contact, boarding_status, house) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING student_id", 
                    (current_user.school_id, data.get('first_name'), data.get('last_name'), data.get('guardian_name'), data.get('guardian_contact'), data.get('boarding_status', 'Day'), data.get('house', 'Unassigned')))
        new_id = cur.fetchone()['student_id']
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"message": f"Student Enrolled! New ID: {new_id}"}), 201
    elif request.method == 'GET':
        cur.execute("SELECT student_id, first_name, last_name, boarding_status, house, guardian_contact FROM students WHERE school_id = %s ORDER BY student_id DESC", (current_user.school_id,))
        students = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"data": students})

@app.route('/api/fees/bill', methods=['POST'])
@login_required
@require_active_subscription
def bill_student():
    data = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO fees (school_id, student_id, fee_category, description, amount_due, academic_year, term) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING fee_id", 
                (current_user.school_id, data.get('student_id'), data.get('fee_category'), data.get('description'), data.get('amount_due'), data.get('academic_year'), data.get('term')))
    new_id = cur.fetchone()['fee_id']
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"{data.get('fee_category')} Bill issued! Reference Fee ID: {new_id}"}), 201

@app.route('/api/fees/pay', methods=['POST'])
@login_required
@require_active_subscription
def log_payment():
    data = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO payments (fee_id, amount_paid, payment_method) VALUES (%s, %s, %s)", (data.get('fee_id'), data.get('amount_paid'), data.get('payment_method')))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Payment logged securely to ledger!"}), 201

@app.route('/api/grades', methods=['POST'])
@login_required
@require_active_subscription
def add_grade():
    data = request.get_json()
    class_score = int(data.get('class_score', 0))
    exam_score = int(data.get('exam_score', 0))
    total_score = class_score + exam_score
    waec = get_waec_grade(total_score)
    remarks = data.get('remarks', '')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT subject_id FROM subjects WHERE subject_name = %s AND school_id = %s", (data.get('subject_name'), current_user.school_id))
    sub = cur.fetchone()
    if not sub:
        cur.execute("INSERT INTO subjects (school_id, subject_name) VALUES (%s, %s) RETURNING subject_id", (current_user.school_id, data.get('subject_name')))
        sub_id = cur.fetchone()['subject_id']
    else:
        sub_id = sub['subject_id']
        
    cur.execute("INSERT INTO grades (school_id, student_id, subject_id, class_score, exam_score, total_score, waec_grade, academic_year, term, teacher_remarks) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (current_user.school_id, data.get('student_id'), sub_id, class_score, exam_score, total_score, waec, data.get('academic_year'), data.get('term'), remarks))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"SBA Recorded! Total: {total_score}% ({waec})"})

@app.route('/api/report_card/<int:student_id>', methods=['GET'])
@login_required
@require_active_subscription
def get_report_card(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return jsonify({"error": "Access Denied."}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.teacher_remarks, g.term FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s AND g.school_id = %s", (student_id, current_user.school_id))
    grades = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"grades": grades})

@app.route('/api/statement/<int:student_id>', methods=['GET'])
@login_required
@require_active_subscription
def get_statement(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return jsonify({"error": "Access Denied."}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    query = """
    SELECT f.fee_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due, 
    COALESCE(SUM(p.amount_paid), 0) as total_paid, 
    (f.amount_due - COALESCE(SUM(p.amount_paid), 0)) as remaining_balance 
    FROM fees f 
    LEFT JOIN payments p ON f.fee_id = p.fee_id 
    WHERE f.student_id = %s AND f.school_id = %s 
    GROUP BY f.fee_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due
    ORDER BY f.date_issued DESC
    """
    cur.execute(query, (student_id, current_user.school_id))
    statement = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"statement": statement}), 200

@app.route('/api/register_staff', methods=['POST'])
@login_required
@require_active_subscription
def register_staff():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    data = request.get_json()
    hashed = generate_password_hash(data.get('password'))
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO system_users (school_id, email, password_hash, role, linked_student_id) VALUES (%s, %s, %s, %s, %s)", 
                    (current_user.school_id, data.get('email'), hashed, data.get('role'), data.get('linked_student_id') or None))
        conn.commit()
        return jsonify({"message": f"{data.get('role').capitalize()} account created!"}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Email already exists or invalid ID."}), 409
    finally:
        cur.close(); conn.close()

# --- 8. THE FRONTEND DASHBOARD ---
@app.route('/dashboard')
def dashboard():
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ghana SMS | Enterprise Portal</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root { --primary: #0f4c81; --secondary: #f4f7f6; --accent: #28a745; --text: #333; --danger: #dc3545; --info: #17a2b8;}
            body { font-family: 'Segoe UI', system-ui, sans-serif; background-color: var(--secondary); margin: 0; display: flex; color: var(--text); }
            .sidebar { width: 250px; background: var(--primary); color: white; min-height: 100vh; padding: 20px; box-sizing: border-box; position: fixed; overflow-y: auto;}
            .sidebar h2 { margin-top: 0; border-bottom: 1px solid rgba(255,255,255,0.2); padding-bottom: 10px; font-size: 1.2rem; }
            .sidebar button { background: rgba(255,255,255,0.1); color: white; border: none; padding: 12px; width: 100%; text-align: left; margin-bottom: 5px; border-radius: 4px; cursor: pointer; transition: 0.3s; }
            .sidebar button:hover { background: rgba(255,255,255,0.2); }
            .user-info { font-size: 0.85rem; color: #a5c3e0; margin-bottom: 20px; word-wrap: break-word;}
            .main-content { margin-left: 250px; flex: 1; padding: 40px; box-sizing: border-box; min-height: 100vh; }
            .card { background: white; padding: 25px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 20px; }
            h3 { margin-top: 0; color: var(--primary); border-bottom: 2px solid #eee; padding-bottom: 8px;}
            input, select { width: 100%; padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
            .btn { background: var(--primary); color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; margin-bottom: 10px;}
            .btn-success { background: var(--accent); }
            .btn-danger { background: var(--danger); }
            .btn-info { background: var(--info); }
            .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            #toast { display: none; position: fixed; bottom: 30px; right: 30px; padding: 15px 25px; color: white; background: var(--accent); border-radius: 5px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); z-index: 1000; font-weight: bold; }
            
            .table-container { overflow-x: auto; margin-top: 15px; border: 1px solid #ddd; border-radius: 5px; max-height: 400px; }
            table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.95rem; }
            th { background: var(--primary); color: white; padding: 12px; position: sticky; top: 0; }
            td { padding: 12px; border-bottom: 1px solid #eee; }
            tr:nth-child(even) td { background: #f8f9fa; }
            #searchInput { display: none; margin-bottom: 10px; border: 2px solid var(--primary); }
            .chart-container { position: relative; height: 300px; width: 100%; }
        </style>
    </head>
    <body>
        <div id="toast">Message</div>
        <div class="sidebar">
            <h2>SaaS Engine</h2>
            {% if current_user.is_authenticated %}
                <div class="user-info">Logged in as:<br><b>{{ current_user.email }}</b><br>Role: <span style="text-transform: uppercase;">{{ current_user.role }}</span></div>
                
                {% if current_user.role == 'superadmin' %}
                    <button onclick="loadSchools()">🏢 Global Tenants</button>
                    <button class="btn-success" onclick="sendAction('/api/setup_db', {}, true)">Sync SaaS Database</button>
                {% elif current_user.role == 'admin' %}
                    <button onclick="document.getElementById('analytics-section').scrollIntoView()">Live Analytics</button>
                    <button onclick="document.getElementById('admissions-section').scrollIntoView()">Admissions Desk</button>
                    <button onclick="document.getElementById('finance-section').scrollIntoView()">Financial Desk</button>
                    <button onclick="document.getElementById('academics-section').scrollIntoView()">Academics & SBA</button>
                    <button onclick="document.getElementById('admin-tools').scrollIntoView()">Admin Tools</button>
                {% else %}
                    <button onclick="document.getElementById('academics-section').scrollIntoView()">My Portal</button>
                {% endif %}
                <br><br><button class="btn-danger" onclick="logout()">Secure Logout</button>
            {% else %}
                <button class="btn-success" onclick="sendAction('/api/setup_db', {}, true)">1. Sync SaaS Database</button>
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
                <h3>🚀 Onboard New School Tenant</h3>
                <div class="grid-2">
                    <div>
                        <input type="text" id="onboardSchool" placeholder="School Name (e.g. Accra Academy)">
                        <input type="email" id="onboardEmail" placeholder="Initial Admin Email">
                    </div>
                    <div>
                        <input type="password" id="onboardPass" placeholder="Initial Admin Password">
                        <button class="btn btn-success" onclick="onboardNewSchool()">Provision New Tenant</button>
                    </div>
                </div>
            </div>
            <div class="card">
                <h3>Global Tenant Control Room</h3>
                <button class="btn" onclick="loadSchools()">Refresh Tenant List</button>
                <div id="school-container"></div>
            </div>

            {% else %}
            
            {% if current_user.role == 'admin' %}
            <div id="analytics-section" class="card">
                <h3>Live Financial Analytics</h3>
                <div class="chart-container"><canvas id="financeChart"></canvas></div>
            </div>
            {% endif %}

            {% if current_user.role in ['admin', 'teacher'] %}
            <div id="admissions-section" class="card grid-2">
                <div>
                    <h3>Enroll New Student</h3>
                    <div class="grid-2">
                        <input type="text" id="sFirst" placeholder="First Name">
                        <input type="text" id="sLast" placeholder="Last Name">
                    </div>
                    <div class="grid-2">
                        <select id="sBoarding">
                            <option value="Day">Day Student</option>
                            <option value="Boarding">Boarding Student</option>
                        </select>
                        <input type="text" id="sHouse" placeholder="House (e.g. Nkrumah House)">
                    </div>
                    <div class="grid-2">
                        <input type="text" id="sGName" placeholder="Guardian Name">
                        <input type="text" id="sGContact" placeholder="Guardian Contact">
                    </div>
                    <button class="btn btn-success" onclick="sendAction('/api/students', {first_name: document.getElementById('sFirst').value, last_name: document.getElementById('sLast').value, guardian_name: document.getElementById('sGName').value, guardian_contact: document.getElementById('sGContact').value, boarding_status: document.getElementById('sBoarding').value, house: document.getElementById('sHouse').value})">Register Student</button>
                </div>
                <div>
                    <h3>Student Directory</h3>
                    <button class="btn" onclick="loadRoster()">Load Enrolled Roster</button>
                </div>
            </div>
            {% endif %}

            {% if current_user.role == 'admin' %}
            <div id="finance-section" class="card grid-2">
                <div>
                    <h3>1. Issue Segmented Bill</h3>
                    <input type="number" id="bStuId" placeholder="Student ID">
                    <select id="bCat">
                        <option value="Consolidated Fee">Consolidated Term Fee (All-Inclusive)</option>
                        <option value="Tuition">Tuition Fee</option>
                        <option value="PTA Dues">PTA Dues</option>
                        <option value="Feeding Fee">Feeding Fee</option>
                        <option value="Exams Fee">Exams Fee</option>
                        <option value="Bus Fee">Bus Fee</option>
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
                    <h3>2. Record Payment</h3>
                    <input type="number" id="pFeeId" placeholder="Fee ID (from Bill)">
                    <input type="number" id="pAmount" placeholder="Amount Paid (GHS)">
                    <select id="pMethod">
                        <option value="Cash">Cash</option>
                        <option value="Mobile Money (MoMo)">Mobile Money (MoMo)</option>
                        <option value="Bank Transfer">Bank Transfer</option>
                    </select>
                    <button class="btn btn-success" onclick="sendAction('/api/fees/pay', {fee_id: document.getElementById('pFeeId').value, amount_paid: document.getElementById('pAmount').value, payment_method: document.getElementById('pMethod').value})">Log Payment</button>
                </div>
            </div>
            {% endif %}

            <div class="card" id="data-viewer" style="display: none; border: 2px solid var(--primary);">
                <h3 id="viewer-title">Data Explorer</h3>
                <input type="text" id="searchInput" onkeyup="filterTable()" placeholder="🔍 Search records instantly...">
                <div id="table-container" class="table-container"></div>
            </div>

            <div id="academics-section" class="card grid-2">
                {% if current_user.role in ['admin', 'teacher'] %}
                <div>
                    <h3>Record SBA & Exam Grades</h3>
                    <input type="number" id="gStuId" placeholder="Student ID">
                    <input type="text" id="gSub" placeholder="Subject Name (e.g. Mathematics)">
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
                {% endif %}

                <div>
                    <h3>Student Reports & Financials</h3>
                    {% if current_user.role == 'guardian' %}
                        <p style="color: #666; font-size: 0.9rem;">Viewing records for Student ID: {{ current_user.linked_student_id }}</p>
                        <input type="hidden" id="repId" value="{{ current_user.linked_student_id }}">
                    {% else %}
                        <input type="number" id="repId" placeholder="Student ID">
                    {% endif %}
                    
                    <button class="btn" onclick="loadReport()">View Term Report</button>
                    <button class="btn btn-success" onclick="loadStatement()">View Outstanding Balance</button>
                </div>
            </div>

            {% if current_user.role == 'admin' %}
            <div id="admin-tools" class="card grid-2">
                <div>
                    <h3>Register New Teacher</h3>
                    <input type="email" id="tEmail" placeholder="Teacher Email">
                    <input type="password" id="tPass" placeholder="Teacher Password">
                    <button class="btn" onclick="sendAction('/api/register_staff', {email: document.getElementById('tEmail').value, password: document.getElementById('tPass').value, role: 'teacher', linked_student_id: null})">Create Teacher</button>
                </div>
                <div>
                    <h3>Register Guardian Portal</h3>
                    <input type="email" id="pEmail" placeholder="Parent/Guardian Email">
                    <input type="password" id="pPass" placeholder="Parent Password">
                    <input type="number" id="pStuId" placeholder="Linked Student ID">
                    <button class="btn btn-success" onclick="sendAction('/api/register_staff', {email: document.getElementById('pEmail').value, password: document.getElementById('pPass').value, role: 'guardian', linked_student_id: document.getElementById('pStuId').value})">Link Guardian to Student</button>
                </div>
            </div>
            {% endif %}

            {% endif %}
        </main>

        <script>
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
                    if (res.ok) { showToast(data.message); if (endpoint === '/api/setup_db') loadSchools(); }
                    else if (res.status === 402) showToast(data.error, true); 
                    else showToast(data.error || "Error", true);
                } catch(e) { showToast("Connection failed", true); }
            }

            function renderTable(title, headers, rows, keys) {
                const viewer = document.getElementById('data-viewer');
                document.getElementById('viewer-title').innerText = title;
                const searchInput = document.getElementById('searchInput');
                const container = document.getElementById('table-container');
                
                if (!rows || rows.length === 0) {
                    container.innerHTML = '<div style="padding: 20px;">No records found.</div>';
                    searchInput.style.display = 'none';
                    viewer.style.display = 'block';
                    return;
                }
                
                searchInput.style.display = 'block';
                searchInput.value = '';
                
                let html = '<table id="dataTable"><tr>';
                headers.forEach(h => html += `<th>${h}</th>`);
                html += '</tr>';
                
                rows.forEach(row => {
                    html += '<tr>';
                    keys.forEach(k => {
                        let val = row[k];
                        if (k === 'remaining_balance' && val > 0) {
                            html += `<td style="color:#dc3545; font-weight:bold;">${val}</td>`;
                        } else if (k === 'boarding_status') {
                            let badge = val === 'Boarding' ? 'background:#0f4c81;color:white;padding:3px 8px;border-radius:12px;font-size:0.8rem;' : 'background:#eee;padding:3px 8px;border-radius:12px;font-size:0.8rem;';
                            html += `<td><span style="${badge}">${val}</span></td>`;
                        } else {
                            html += `<td>${val}</td>`;
                        }
                    });
                    html += '</tr>';
                });
                html += '</table>';
                
                container.innerHTML = html;
                viewer.style.display = 'block';
                viewer.scrollIntoView({behavior: "smooth"});
            }

            function filterTable() {
                let input = document.getElementById("searchInput");
                let filter = input.value.toUpperCase();
                let table = document.getElementById("dataTable");
                let tr = table.getElementsByTagName("tr");
                for (let i = 1; i < tr.length; i++) {
                    let rowText = tr[i].innerText.toUpperCase();
                    if (rowText.indexOf(filter) > -1) { tr[i].style.display = ""; } 
                    else { tr[i].style.display = "none"; }
                }
            }

            async function loadRoster() {
                const res = await fetch('/api/students');
                const data = await res.json();
                if (res.status === 402) { showToast(data.error, true); return; }
                renderTable("Student Roster (Live Search)", ['ID', 'First Name', 'Last Name', 'Status', 'House', 'Guardian Contact'], data.data, ['student_id', 'first_name', 'last_name', 'boarding_status', 'house', 'guardian_contact']);
            }

            async function loadReport() {
                const id = document.getElementById('repId').value;
                const res = await fetch('/api/report_card/' + id);
                if (!res.ok) { showToast("Access Denied or Not Found", true); return; }
                const data = await res.json();
                renderTable("Terminal Report Card", ['Subject', 'Class (30)', 'Exam (70)', 'Total (100)', 'Grade', 'Remarks', 'Term'], data.grades, ['subject_name', 'class_score', 'exam_score', 'total_score', 'waec_grade', 'teacher_remarks', 'term']);
            }

            async function loadStatement() {
                const id = document.getElementById('repId').value;
                const res = await fetch('/api/statement/' + id);
                if (!res.ok) { showToast("Access Denied", true); return; }
                const data = await res.json();
                renderTable("Segmented Arrears & Ledger", ['Fee ID', 'Category', 'Year', 'Term', 'Due (GHS)', 'Paid (GHS)', 'Arrears (GHS)'], data.statement, ['fee_id', 'fee_category', 'academic_year', 'term', 'amount_due', 'total_paid', 'remaining_balance']);
            }

            async function onboardNewSchool() {
                const payload = {
                    school_name: document.getElementById('onboardSchool').value,
                    admin_email: document.getElementById('onboardEmail').value,
                    admin_password: document.getElementById('onboardPass').value
                };
                const res = await fetch('/api/superadmin/onboard', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message);
                    document.getElementById('onboardSchool').value = '';
                    document.getElementById('onboardEmail').value = '';
                    document.getElementById('onboardPass').value = '';
                    loadSchools();
                } else { showToast(data.error, true); }
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

            async function loadSchools() {
                const res = await fetch('/api/superadmin/schools');
                if(!res.ok) return;
                const data = await res.json();
                let html = '<table><tr><th>ID</th><th>School Name</th><th>Expiry Date</th><th>Status</th><th>Actions</th></tr>';
                data.data.forEach(s => {
                    const statusColor = s.status === 'Active' ? 'green' : 'red';
                    html += `<tr>
                        <td>${s.school_id}</td><td>${s.school_name}</td><td>${s.expiry_date}</td>
                        <td style="color:${statusColor}; font-weight:bold;">${s.status}</td>
                        <td>
                            <button class="btn btn-success" style="width: auto; padding: 6px 12px; margin-right: 5px; margin-bottom: 0;" onclick="sendAction('/api/superadmin/renew/${s.school_id}', {})">Renew</button>
                            <button class="btn btn-info" style="width: auto; padding: 6px 12px; margin-right: 5px; margin-bottom: 0;" onclick="window.location.href='/api/superadmin/backup/${s.school_id}'">⬇️ Backup</button>
                            <input type="file" id="file_${s.school_id}" accept=".json" style="display:none;" onchange="uploadRestore(${s.school_id})">
                            <button class="btn btn-danger" style="width: auto; padding: 6px 12px; margin-bottom: 0;" onclick="document.getElementById('file_${s.school_id}').click()">⬆️ Restore</button>
                        </td>
                    </tr>`;
                });
                html += '</table>';
                document.getElementById('school-container').innerHTML = html;
            }

            window.onload = async function() {
                if (document.getElementById('school-container')) loadSchools();
                if (document.getElementById('financeChart')) {
                    try {
                        const res = await fetch('/api/analytics');
                        const data = await res.json();
                        const ctx = document.getElementById('financeChart').getContext('2d');
                        new Chart(ctx, {
                            type: 'doughnut',
                            data: {
                                labels: ['Total Collected (GHS)', 'Outstanding Balances (GHS)'],
                                datasets: [{
                                    data: [data.financials.paid, data.financials.outstanding],
                                    backgroundColor: ['#28a745', '#dc3545'],
                                    borderWidth: 0
                                }]
                            },
                            options: { responsive: true, maintainAspectRatio: false, cutout: '70%' }
                        });
                    } catch(e) { console.log("Analytics loading error", e) }
                }
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, current_user=current_user)

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
