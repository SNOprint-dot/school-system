import os
import csv
import json
import psycopg2
import base64
import uuid
import requests
from io import StringIO
from functools import wraps
from datetime import datetime
from flask import Flask, jsonify, request, render_template, Response, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from db_config import get_db_connection
from db_engine import initialize_database, automated_weekly_backup, User, get_waec_grade, s3_client, AWS_BUCKET_NAME, custom_json_serializer

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secure-enterprise-key')

login_manager = LoginManager()
login_manager.init_app(app)

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

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

# --- URL ROUTING FIX ---
@app.route('/')
def index():
    # Automatically forces users to the dashboard
    return redirect(url_for('dashboard'))

# --- AUTH & SECURITY ENGINE ---
@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT user_id, email, password_hash, role, linked_student_id, school_id FROM system_users WHERE email = %s", (data.get('email'),))
        user_data = cur.fetchone()
        cur.close()
        conn.close()
        
        if user_data and check_password_hash(user_data['password_hash'], data.get('password')):
            user = User(user_data['user_id'], user_data['email'], user_data['role'], user_data['linked_student_id'], user_data['school_id'])
            login_user(user)
            return jsonify({"message": f"Logged in as {user.role}."})
            
        return jsonify({"error": "Invalid credentials!"}), 401
    except Exception as e:
        return jsonify({"error": f"Login Engine Error: {str(e)}"}), 500

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
        conn.commit(); return jsonify({"message": "Your password has been updated!"}), 200
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/admin/reset_password', methods=['POST'])
@login_required
def admin_reset_password():
    if current_user.role != 'admin': return jsonify({"error": "Admin clearance required."}), 403
    d = request.get_json(); new_hash = generate_password_hash(d.get('new_password')); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE system_users SET password_hash = %s WHERE email = %s AND school_id = %s RETURNING user_id", (new_hash, d.get('target_email'), current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": "User email not found in school directory."}), 404
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Forced Password Reset', f"Target: {d.get('target_email')}"))
        conn.commit(); return jsonify({"message": f"Password reset for {d.get('target_email')}!"}), 200
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

@app.route('/api/audit_logs', methods=['GET'])
@login_required
def get_audit_logs():
    if current_user.role != 'admin': return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT TO_CHAR(timestamp, 'YYYY-MM-DD HH24:MI:SS') as time, user_email, action, target FROM audit_logs WHERE school_id = %s ORDER BY timestamp DESC LIMIT 200", (current_user.school_id,))
    logs = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": logs})

# --- SUPER ADMIN MASTER SETTINGS ---
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
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
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

# --- CORE TENANT OPERATIONS ---
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
        conn.commit(); cur.close(); conn.close(); return jsonify({"message": f"Student Enrolled successfully! ID: {new_id}"}), 201
    else:
        cur.execute("SELECT student_id, first_name, last_name, current_class, transport_route, boarding_status, house, guardian_contact FROM students WHERE school_id = %s ORDER BY student_id DESC", (current_user.school_id,))
        students = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": students})

@app.route('/api/fees/bill', methods=['POST'])
@login_required
def bill_student():
    d = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    try:
        stu_id = int(d.get('student_id') or 0); amt = float(d.get('amount_due') or 0.0)
        cur.execute("SELECT student_id FROM students WHERE student_id = %s AND school_id = %s", (stu_id, current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": f"Student ID {stu_id} does not exist!"}), 404
        cur.execute("INSERT INTO fees (school_id, student_id, fee_category, description, amount_due, academic_year, term) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                    (current_user.school_id, stu_id, d.get('fee_category'), str(d.get('description') or '')[:250], amt, str(d.get('academic_year') or '')[:9], str(d.get('term') or '')[:20]))
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Issued Bill', f"GHS {amt} to ID {stu_id}"))
        conn.commit(); return jsonify({"message": "Bill issued successfully!"}), 201
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 400
    finally: cur.close(); conn.close()

@app.route('/api/momo/initialize', methods=['POST'])
@login_required
def momo_initialize():
    d = request.get_json(); fee_id = int(d.get('fee_id')); amount = float(d.get('amount')); phone = str(d.get('phone') or '').strip()
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT student_id, amount_due FROM fees WHERE fee_id = %s", (fee_id,))
    fee = cur.fetchone()
    if not fee: cur.close(); conn.close(); return jsonify({"error": "Fee bill not found."}), 404

    if PAYSTACK_SECRET_KEY:
        try:
            url = "https://api.paystack.co/transaction/initialize"
            headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
            payload = {"email": current_user.email, "amount": int(amount * 100), "currency": "GHS", "channels": ["mobile_money", "card"], "metadata": {"fee_id": fee_id, "student_id": fee['student_id'], "phone": phone}}
            res = requests.post(url, json=payload, headers=headers).json()
            cur.close(); conn.close()
            if res.get('status'): return jsonify({"status": "paystack_redirect", "auth_url": res['data']['authorization_url']})
            else: return jsonify({"error": res.get('message', 'Paystack rejected transaction.')}), 400
        except Exception as e: cur.close(); conn.close(); return jsonify({"error": f"Gateway error: {str(e)}"}), 500
    else:
        try:
            cur.execute("INSERT INTO payments (fee_id, amount_paid, payment_method) VALUES (%s, %s, %s)", (fee_id, amount, f"MTN MoMo Sandbox ({phone})"))
            cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'MoMo Payment (Sandbox)', f"Paid GHS {amount} for Fee ID {fee_id}"))
            conn.commit(); cur.close(); conn.close(); return jsonify({"status": "cleared", "message": f"[SANDBOX MODE] Simulated USSD prompt approved for {phone}! GHS {amount} credited."}), 200
        except Exception as e: conn.rollback(); cur.close(); conn.close(); return jsonify({"error": str(e)}), 500

@app.route('/api/debtors', methods=['GET'])
@login_required
def get_debtors():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT s.student_id, s.first_name, s.last_name, s.guardian_contact, (SUM(f.amount_due) - COALESCE((SELECT SUM(amount_paid) FROM payments p JOIN fees f2 ON p.fee_id = f2.fee_id WHERE f2.student_id = s.student_id), 0)) as arrears FROM students s JOIN fees f ON s.student_id = f.student_id WHERE s.school_id = %s GROUP BY s.student_id, s.first_name, s.last_name, s.guardian_contact HAVING (SUM(f.amount_due) - COALESCE((SELECT SUM(amount_paid) FROM payments p JOIN fees f2 ON p.fee_id = f2.fee_id WHERE f2.student_id = s.student_id), 0)) > 0 ORDER BY arrears DESC", (current_user.school_id,))
    debtors = cur.fetchall(); cur.close(); conn.close(); return jsonify({"data": debtors})

@app.route('/api/statement/<int:student_id>', methods=['GET'])
@login_required
def get_statement(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return jsonify({"error": "Access Denied."}), 403
    conn = get_db_connection(); cur = conn.cursor()
    query = "SELECT f.fee_id, f.student_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due, COALESCE(SUM(p.amount_paid), 0) as total_paid, (f.amount_due - COALESCE(SUM(p.amount_paid), 0)) as remaining_balance FROM fees f LEFT JOIN payments p ON f.fee_id = p.fee_id WHERE f.student_id = %s AND f.school_id = %s GROUP BY f.fee_id, f.student_id, f.fee_category, f.academic_year, f.term, f.description, f.amount_due ORDER BY f.date_issued DESC"
    cur.execute(query, (student_id, current_user.school_id))
    statement = cur.fetchall(); cur.close(); conn.close(); return jsonify({"statement": statement}), 200

# --- FRONTEND CLIENT ROUTE ---
@app.route('/dashboard')
def dashboard():
    school_name = "Global ERP Engine"; primary_color = "#0f4c81"
    if current_user.is_authenticated and current_user.school_id:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT school_name, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
        inst = cur.fetchone(); cur.close(); conn.close()
        if inst: school_name = inst['school_name']; primary_color = inst['primary_color'] or "#0f4c81"
    return render_template('dashboard.html', school_name=school_name, primary_color=primary_color)

# --- BOOT SEQUENCE ---
with app.app_context():
    try:
        initialize_database()
        print("SaaS Database Engine synchronized successfully on boot.")
    except Exception as e:
        print(f"Boot synchronization skipped: {e}")

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
