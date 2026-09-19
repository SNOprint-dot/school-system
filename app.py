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
from flask import Flask, jsonify, request, render_template, render_template_string, Response, redirect, url_for
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

# --- URL ROUTING ---
@app.route('/')
def index():
    return redirect(url_for('dashboard'))

# --- AUTH & SECURITY ENGINE ---
@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, email, password_hash, role, linked_student_id, school_id FROM system_users WHERE email = %s", (data.get('email'),))
        user_data = cur.fetchone(); cur.close(); conn.close()
        
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

@app.route('/api/superadmin/reset_admin', methods=['POST'])
@login_required
def superadmin_reset_admin():
    if current_user.role != 'superadmin': return jsonify({"error": "Unauthorized"}), 403
    d = request.get_json(); new_hash = generate_password_hash(d.get('new_password')); conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE system_users SET password_hash = %s WHERE email = %s RETURNING user_id", (new_hash, d.get('admin_email')))
        if not cur.fetchone(): return jsonify({"error": "Admin email not found in global registry."}), 404
        conn.commit(); return jsonify({"message": f"Global Override successful for {d.get('admin_email')}!"}), 200
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
        conn.commit(); return jsonify({"message": "Master Super Admin credentials updated!"}), 200
    except psycopg2.IntegrityError: conn.rollback(); return jsonify({"error": "Email already in use."}), 409
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

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

# --- CORE OPERATIONS (STUDENTS, FEES, EXPENSES) ---
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
        except Exception as e: conn.rollback(); return jsonify({"error": f"Update failed: {str(e)}"}), 500
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
        except Exception as e: conn.rollback(); return jsonify({"error": f"Database Error: {str(e)}"}), 500
        finally: cur.close(); conn.close()

@app.route('/api/students/promote', methods=['POST'])
@login_required
def promote_students():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    d = request.get_json()
    from_class = str(d.get('from_class') or '').strip(); to_class = str(d.get('to_class') or '').strip()
    if not from_class or not to_class: return jsonify({"error": "Provide both classes."}), 400
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("UPDATE students SET current_class = %s WHERE current_class = %s AND school_id = %s RETURNING student_id", (to_class, from_class, current_user.school_id))
        promoted = cur.fetchall()
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Promoted Cohort', f"Moved {len(promoted)} from {from_class} to {to_class}"))
        conn.commit(); return jsonify({"message": f"Success! Promoted {len(promoted)} students."}), 200
    except Exception as e: conn.rollback(); return jsonify({"error": str(e)}), 500
    finally: cur.close(); conn.close()

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

@app.route('/api/fees/pay', methods=['POST'])
@login_required
def log_payment():
    d = request.get_json(); conn = get_db_connection(); cur = conn.cursor()
    try:
        fee_id = int(d.get('fee_id') or 0); amt = float(d.get('amount_paid') or 0.0)
        cur.execute("SELECT fee_id FROM fees WHERE fee_id = %s AND school_id = %s", (fee_id, current_user.school_id))
        if not cur.fetchone(): return jsonify({"error": f"Fee ID {fee_id} does not exist! Check the Statement."}), 404
        cur.execute("INSERT INTO payments (fee_id, amount_paid, payment_method) VALUES (%s, %s, %s)", (fee_id, amt, d.get('payment_method')))
        cur.execute("INSERT INTO audit_logs (school_id, user_email, action, target) VALUES (%s, %s, %s, %s)", (current_user.school_id, current_user.email, 'Logged Payment', f"GHS {amt} via {d.get('payment_method')} (Fee {fee_id})"))
        conn.commit(); return jsonify({"message": "Payment logged securely!"}), 201
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

# --- STATIC DOCUMENTS & IMAGES ---
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
    return Response('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" style="background:var(--primary); padding:10px; border-radius:12px;"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>', mimetype='image/svg+xml')

@app.route('/print_ids', methods=['GET'])
@login_required
def print_ids():
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT student_id, first_name, last_name, current_class, boarding_status, house FROM students WHERE school_id = %s ORDER BY student_id", (current_user.school_id,))
    students = cur.fetchall()
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone(); cur.close(); conn.close(); primary_color = inst['primary_color'] or '#0f4c81'
    
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
    </style></head><body><button class='no-print' onclick='window.print()' style='padding:10px; margin-bottom:20px; cursor:pointer;'>🖨️ Print IDs</button><div class='page'>"""
    for s in students:
        c_name = s.get('current_class') or 'N/A'; qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=70x70&data=VERIFIED:{inst['school_name']}:STU-{s['student_id']}"
        html += f"""<div class='id-card'><div class='header'>{inst['school_name']}</div><div class='photo-box'><img src='/api/photo/{s['student_id']}' style='width:100%; height:100%; object-fit:cover;'></div><div class='details'><strong>Name:</strong> {s['first_name']} {s['last_name']}<br><strong>ID:</strong> {inst['school_name'][:3].upper()}-{s['student_id']:04d}<br><strong>Class:</strong> {c_name}<br><strong>Status:</strong> {s['boarding_status']}</div><div class='qr-box'><img src='{qr_url}' style='width:100%; height:100%;'></div><div class='footer'>CONTACT: {inst['phone']} | {inst['address']}</div></div>"""
    html += "</div></body></html>"
    return html

@app.route('/print_report/<int:student_id>', methods=['GET'])
@login_required
def print_report(student_id):
    if current_user.role == 'guardian' and current_user.linked_student_id != student_id: return "Access Denied.", 403
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
    student = cur.fetchone()
    if not student: return "Student not found.", 404
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone(); primary_color = inst['primary_color'] or '#0f4c81'
    cur.execute("SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.teacher_remarks, g.term, g.academic_year FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s AND g.school_id = %s ORDER BY g.academic_year DESC, g.term DESC, sub.subject_name", (student_id, current_user.school_id))
    grades = cur.fetchall(); cur.close(); conn.close()

    rows = ""
    for g in grades: rows += f"<tr><td>{g['subject_name']}</td><td>{g['class_score']}</td><td>{g['exam_score']}</td><td><strong>{g['total_score']}</strong></td><td><strong>{g['waec_grade']}</strong></td><td>{g['teacher_remarks']}</td><td>{g['term']} ({g['academic_year']})</td></tr>"
    if not rows: rows = "<tr><td colspan='7' style='text-align:center;'>No academic records found.</td></tr>"

    html = f"""<!DOCTYPE html><html><head><title>Report | {student['first_name']} {student['last_name']}</title><style>:root {{ --primary: {primary_color}; }} body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #eee; padding: 20px; color: #333; }} .page {{ background: white; max-width: 900px; margin: auto; padding: 40px; border-radius: 8px; }} .header {{ display: flex; justify-content: space-between; border-bottom: 3px solid var(--primary); padding-bottom: 20px; margin-bottom: 30px; }} table {{ width: 100%; border-collapse: collapse; margin-bottom: 40px; }} th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }} th {{ background: var(--primary); color: white; }} tr:nth-child(even) {{ background-color: #f9f9f9; }} .signatures {{ display: flex; justify-content: space-between; margin-top: 80px; }} .sig-line {{ border-top: 2px solid #333; width: 250px; text-align: center; padding-top: 10px; font-weight: bold; }} @media print {{ body {{ background: white; padding: 0; }} .page {{ box-shadow: none; max-width: 100%; padding: 0; }} .no-print {{ display: none; }} }}</style></head><body><div style="text-align: center;"><button class="no-print" onclick="window.print()" style="padding:10px 20px; cursor:pointer; background:#28a745; color:white; border:none; border-radius:6px; font-weight:bold; margin-bottom:20px;">🖨️ Print Report</button></div><div class="page"><div class="header"><div style="display:flex; gap: 20px;"><img src="/api/logo/{current_user.school_id}" style="width:90px; height:90px; object-fit:contain;"><div><h1 style="color:var(--primary); margin:0; text-transform:uppercase;">{inst['school_name']}</h1><p style="margin:5px 0;">{inst['address']} | Tel: {inst['phone']}</p><h2 style="margin: 0; color: #555;">TERMINAL REPORT</h2></div></div><img src="/api/photo/{student['student_id']}" style="width:110px; height:130px; object-fit:cover; border:2px solid var(--primary);"></div><p><strong>NAME:</strong> {student['first_name'].upper()} {student['last_name'].upper()} &nbsp;&nbsp;|&nbsp;&nbsp; <strong>CLASS:</strong> {student['current_class']} &nbsp;&nbsp;|&nbsp;&nbsp; <strong>STATUS:</strong> {student['boarding_status']}</p><table><tr><th>Subject</th><th>Class (30%)</th><th>Exam (70%)</th><th>Total (100%)</th><th>Grade</th><th>Remarks</th><th>Term</th></tr>{rows}</table><div class="signatures"><div class="sig-line">Class Teacher's Signature</div><div class="sig-line">Headmaster's Signature</div></div></div></body></html>"""
    return html

@app.route('/print_transcript/<int:student_id>', methods=['GET'])
@login_required
def print_transcript(student_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM students WHERE student_id = %s AND school_id = %s", (student_id, current_user.school_id))
    student = cur.fetchone()
    if not student: return "Student record not found.", 404
    cur.execute("SELECT school_name, address, phone, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
    inst = cur.fetchone(); primary_color = inst['primary_color'] or '#0f4c81'
    cur.execute("""SELECT sub.subject_name, g.class_score, g.exam_score, g.total_score, g.waec_grade, g.academic_year, g.term FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s AND g.school_id = %s ORDER BY g.academic_year ASC, g.term ASC, sub.subject_name ASC""", (student_id, current_user.school_id))
    records = cur.fetchall(); cur.close(); conn.close()
    
    rows = ""
    for r in records: rows += f"<tr><td>{r['academic_year']}</td><td>{r['term']}</td><td>{r['subject_name']}</td><td>{r['class_score']}</td><td>{r['exam_score']}</td><td><strong>{r['total_score']}%</strong></td><td><strong>{r['waec_grade']}</strong></td></tr>"
    html = f"""<!DOCTYPE html><html><head><title>Transcript | {student['first_name']} {student['last_name']}</title><style>body {{ font-family: 'Times New Roman', Times, serif; background: #eee; padding: 20px; }} .sheet {{ background: white; max-width: 900px; margin: auto; padding: 50px; border: 3px double #333; position: relative; box-shadow: 0 0 10px rgba(0,0,0,0.1); }} .header {{ text-align: center; border-bottom: 2px solid #333; padding-bottom: 20px; margin-bottom: 30px; }} table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }} th, td {{ border: 1px solid #333; padding: 8px 12px; font-size: 13px; text-align: left; }} th {{ background: #f2f2f2; text-transform: uppercase; }} .stamp-box {{ margin-top: 60px; display: flex; justify-content: space-between; }} .stamp {{ border-top: 1px solid #333; width: 220px; text-align: center; padding-top: 5px; font-weight: bold; font-size: 12px; }}</style></head><body><div style="text-align:center; margin-bottom:20px;"><button onclick="window.print()" style="padding:10px 20px; font-weight:bold; cursor:pointer;">🖨️ Print Transcript</button></div><div class="sheet"><div class="header"><h1 style="margin:0; text-transform:uppercase; font-size:26px; color:{primary_color};">{inst['school_name']}</h1><p style="margin:5px 0;">{inst['address']} | Tel: {inst['phone']}</p><h2 style="margin-top:15px; font-size:18px; text-decoration:underline;">OFFICIAL ACADEMIC TRANSCRIPT</h2></div><p><strong>Candidate:</strong> {student['first_name'].upper()} {student['last_name'].upper()} &nbsp;&nbsp;|&nbsp;&nbsp; <strong>Student ID:</strong> {inst['school_name'][:3].upper()}-{student['student_id']:04d} &nbsp;&nbsp;|&nbsp;&nbsp; <strong>Class:</strong> {student['current_class']}</p><table><tr><th>Academic Year</th><th>Term</th><th>Subject</th><th>Class (30)</th><th>Exam (70)</th><th>Total</th><th>Grade</th></tr>{rows}</table><div class="stamp-box"><div class="stamp">Academic Registrar</div><div class="stamp">Head of Institution / Stamp</div></div></div></body></html>"""
    return html

# --- EMBEDDED RESILIENT DASHBOARD HTML ---
FALLBACK_DASHBOARD_HTML = """<!DOCTYPE html>
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
                <button onclick="secureSection('finance-section')">💰 Financials & MoMo</button>
                <button onclick="secureSection('settings-section')">⚙️ Vault Settings</button>
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
        {% elif current_user.role == 'superadmin' %}
        <div id="sa-settings-section" class="card hidden">
            <h3>⚙️ Master Settings</h3>
            <div class="grid-2"><input type="email" id="saNewEmail" placeholder="New Email"><input type="password" id="saNewPass" placeholder="New Password"></div>
            <button class="btn" style="background:#333;" onclick="updateSACredentials()">Update Master Credentials</button>
        </div>
        <div class="card" style="border: 2px solid var(--accent);">
            <h3>🚀 Provision New School Tenant</h3>
            <div class="grid-2">
                <div><input type="text" id="onboardSchool" placeholder="Official School Name"><input type="email" id="onboardEmail" placeholder="Administrator Email"></div>
                <div><input type="password" id="onboardPass" placeholder="Temporary Password"><button class="btn btn-success" onclick="onboardNewSchool()">Provision SaaS Tenant</button></div>
            </div>
        </div>
        <div class="card">
            <h3>Global Server Tenants</h3>
            <button class="btn" onclick="loadSchools()">🔄 Refresh Server Data</button>
            <div id="school-container"></div>
        </div>
        {% elif current_user.role == 'admin' %}
        <div id="admissions-section" class="card">
            <h3>🎓 Admissions & Student Directory</h3>
            <div class="grid-2">
                <input type="text" id="sFirst" placeholder="First Name">
                <input type="text" id="sLast" placeholder="Last Name">
            </div>
            <div class="grid-2">
                <input type="text" id="sClass" placeholder="Class / Program">
                <input type="text" id="sGContact" placeholder="Guardian Phone">
            </div>
            <button class="btn btn-success" onclick="enrollStudent()">Register Student</button>
            <hr style="margin:20px 0;">
            <div class="grid-2">
                <div>
                    <input type="number" id="trStuId" placeholder="Student ID">
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
            <button class="btn btn-success" onclick="issueBill()">Issue Bill</button>
            <hr>
            <input type="number" id="stateStuId" placeholder="Target Student ID">
            <button class="btn btn-primary" onclick="loadStatement()">Open Secure Ledger</button>
            <button class="btn btn-warning" onclick="loadDebtors()">Scan Database for Debtors</button>
        </div>
        <div id="settings-section" class="card hidden">
            <h3>⚙️ Security & Settings</h3>
            <div class="grid-2">
                <input type="password" id="myOldPass" placeholder="Current Password">
                <input type="password" id="myNewPass" placeholder="New Password">
            </div>
            <button class="btn" style="background:#333;" onclick="changeMyPassword()">Update Password</button>
        </div>
        {% elif current_user.role == 'guardian' %}
        <div id="guardian-section" class="card">
            <h3>Guardian Portal</h3>
            <div class="grid-2">
                <button class="btn btn-success" onclick="window.open('/print_report/{{ current_user.linked_student_id }}', '_blank')">🖨️ Terminal Report Card</button>
                <button class="btn btn-info" onclick="window.open('/print_transcript/{{ current_user.linked_student_id }}', '_blank')">📜 Full Official Transcript</button>
            </div>
            <hr style="margin:20px 0;">
            <h4>Pay Fees via Mobile Money</h4>
            <div class="grid-2">
                <input type="number" id="momoFeeId" placeholder="Fee ID">
                <input type="number" id="momoAmount" placeholder="Amount (GHS)">
            </div>
            <input type="text" id="momoPhone" placeholder="MoMo Phone Number">
            <button class="btn btn-warning" onclick="initiateMoMo()">Authorize MoMo Payment</button>
        </div>
        {% endif %}
        
        <div class="card hidden" id="data-viewer" style="border: 2px solid var(--primary); margin-top:20px;">
            <div style="background: var(--primary); padding: 15px; display:flex; justify-content:space-between; align-items:center;">
                <h3 id="viewer-title" style="margin:0; color:white;">Data Explorer</h3>
            </div>
            <div id="table-container" style="padding: 15px; overflow-x: auto;"></div>
        </div>
    </main>

    <script>
        let unlockedSections = {};
        function showToast(msg) {
            const t = document.getElementById('toast'); t.innerText = msg; t.style.display = 'block';
            setTimeout(() => t.style.display = 'none', 4000);
        }
        function showSection(id) {
            document.querySelectorAll('.card').forEach(c => { if(c.id && c.id !== 'data-viewer') c.classList.add('hidden'); });
            const target = document.getElementById(id); if(target) target.classList.remove('hidden');
        }
        async function secureSection(sectionId) {
            if (unlockedSections[sectionId]) { showSection(sectionId); return; }
            const pass = prompt("SECURE VAULT: Enter password to proceed.");
            if (!pass) return;
            try {
                const res = await fetch('/api/verify_password', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: pass}) });
                if(res.ok) { unlockedSections[sectionId] = true; showSection(sectionId); showToast("Vault Unlocked"); } 
                else { const data = await res.json(); showToast(data.error, true); }
            } catch(e) { showToast("Connection failed", true); }
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
        async function enrollStudent() {
            const payload = {
                first_name: document.getElementById('sFirst').value, last_name: document.getElementById('sLast').value,
                current_class: document.getElementById('sClass').value, guardian_name: 'Guardian', guardian_contact: document.getElementById('sGContact').value
            };
            sendAction('/api/students', payload);
        }
        async function issueBill() {
            const payload = { student_id: document.getElementById('bStuId').value, amount_due: document.getElementById('bAmount').value, fee_category: 'Tuition', academic_year: '2026', term: 'Term 1', description: 'Academic Bill' };
            sendAction('/api/fees/bill', payload);
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
        function renderTable(title, headers, rows, keys) {
            const viewer = document.getElementById('data-viewer'); document.getElementById('viewer-title').innerText = title; const container = document.getElementById('table-container');
            if (!rows || rows.length === 0) { container.innerHTML = 'No records found.'; viewer.classList.remove('hidden'); return; }
            let html = '<table style="width:100%; border-collapse: collapse;"><tr>';
            headers.forEach(h => html += `<th style="padding:10px; border-bottom: 2px solid #ddd; text-align:left;">${h}</th>`); html += '</tr>';
            rows.forEach(row => {
                html += '<tr>';
                keys.forEach(k => { html += `<td style="padding:10px; border-bottom:1px solid #eee;">${row[k]}</td>`; });
                html += '</tr>';
            });
            container.innerHTML = html + '</table>'; viewer.classList.remove('hidden');
        }
        async function loadStatement() {
            const id = document.getElementById('stateStuId').value;
            const res = await fetch('/api/statement/' + id); const data = await res.json();
            renderTable("Financial Ledger", ['Bill Type', 'Desc', 'Due', 'Paid', 'Balance'], data.statement, ['fee_category', 'description', 'amount_due', 'total_paid', 'remaining_balance']);
        }
        async function loadDebtors() {
            const res = await fetch('/api/debtors'); const data = await res.json();
            renderTable("Debtors", ['ID', 'First Name', 'Last Name', 'Total Owed'], data.data, ['student_id', 'first_name', 'last_name', 'arrears']);
        }
        async function loadSchools() {
            const res = await fetch('/api/superadmin/schools'); const data = await res.json();
            renderTable("Global Tenants", ['ID', 'School Name', 'Expiry', 'Status'], data.data, ['school_id', 'school_name', 'expiry_date', 'status']);
        }
        async function onboardNewSchool() {
            const res = await fetch('/api/superadmin/onboard', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({school_name: document.getElementById('onboardSchool').value, admin_email: document.getElementById('onboardEmail').value, admin_password: document.getElementById('onboardPass').value}) });
            const data = await res.json(); if (res.ok) { showToast(data.message); loadSchools(); } else { showToast(data.error, true); }
        }
        async function updateSACredentials() {
            const res = await fetch('/api/superadmin/credentials', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({new_email: document.getElementById('saNewEmail').value, new_password: document.getElementById('saNewPass').value}) });
            const data = await res.json(); if (res.ok) { showToast(data.message); } else { showToast(data.error, true); }
        }
        async function changeMyPassword() {
            const res = await fetch('/api/change_password', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({current_password: document.getElementById('myOldPass').value, new_password: document.getElementById('myNewPass').value}) });
            const data = await res.json(); if (res.ok) { showToast(data.message); } else { showToast(data.error, true); }
        }
    </script>
</body>
</html>"""

# --- DASHBOARD ROUTE (RESILIENT LOADER) ---
@app.route('/dashboard')
def dashboard():
    school_name = "Global ERP Engine"
    primary_color = "#0f4c81"
    try:
        if current_user.is_authenticated and getattr(current_user, 'school_id', None):
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT school_name, primary_color FROM institutions WHERE school_id = %s", (current_user.school_id,))
            inst = cur.fetchone()
            cur.close()
            conn.close()
            if inst:
                school_name = inst['school_name']
                primary_color = inst['primary_color'] or "#0f4c81"
    except Exception:
        pass
        
    try:
        # Tries loading from templates/dashboard.html first
        return render_template('dashboard.html', school_name=school_name, primary_color=primary_color)
    except Exception:
        # Automatically falls back to embedded dashboard if template file is missing on Render
        return render_template_string(FALLBACK_DASHBOARD_HTML, school_name=school_name, primary_color=primary_color)

# --- BOOT SEQUENCE ---
with app.app_context():
    try:
        initialize_database()
        print("SaaS Database Engine synchronized successfully on boot.")
    except Exception as e:
        print(f"Boot synchronization skipped: {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=automated_weekly_backup, trigger="cron", day_of_week='sun', hour=23, minute=59)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
