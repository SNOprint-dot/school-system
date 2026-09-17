import os
import psycopg2
import csv
from io import StringIO
from functools import wraps
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string, Response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from db_config import get_db_connection

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secure-enterprise-key')

login_manager = LoginManager()
login_manager.init_app(app)

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

# --- 2. THE SAAS BOUNCER (YEARLY BILLING LOCK) ---
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

# --- 3. SYSTEM SETUP (MULTI-TENANT) ---
@app.route('/api/setup_db')
def setup_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # SAAS Master Table
    cur.execute("CREATE TABLE IF NOT EXISTS institutions (school_id SERIAL PRIMARY KEY, school_name VARCHAR(150) NOT NULL UNIQUE, subscription_expiry_date DATE NOT NULL)")
    
    # Multi-Tenant Core Tables
    cur.execute("CREATE TABLE IF NOT EXISTS students (student_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, first_name VARCHAR(100) NOT NULL, last_name VARCHAR(100) NOT NULL, guardian_name VARCHAR(100) NOT NULL, guardian_contact VARCHAR(20) NOT NULL, enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS system_users (user_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, email VARCHAR(100) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, linked_student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE)")
    cur.execute("CREATE TABLE IF NOT EXISTS subjects (subject_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, subject_name VARCHAR(100) NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS grades (grade_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, subject_id INTEGER REFERENCES subjects(subject_id) ON DELETE CASCADE, score INTEGER NOT NULL, waec_grade VARCHAR(2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS fees (fee_id SERIAL PRIMARY KEY, school_id INTEGER REFERENCES institutions(school_id) ON DELETE CASCADE, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, description VARCHAR(255) NOT NULL, amount_due DECIMAL(10, 2) NOT NULL, date_issued TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS payments (payment_id SERIAL PRIMARY KEY, fee_id INTEGER REFERENCES fees(fee_id) ON DELETE CASCADE, amount_paid DECIMAL(10, 2) NOT NULL, payment_method VARCHAR(50) NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    
    # Auto-Create Super Admin (You) and a Test School (Winneba High)
    cur.execute("SELECT * FROM system_users WHERE role = 'superadmin'")
    if not cur.fetchone():
        hashed_sa = generate_password_hash('ceo123')
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s)", ('superadmin@engine.com', hashed_sa, 'superadmin'))
        
        cur.execute("INSERT INTO institutions (school_name, subscription_expiry_date) VALUES (%s, CURRENT_DATE + INTERVAL '30 days') RETURNING school_id", ('Winneba High School',))
        new_school_id = cur.fetchone()['school_id']
        
        hashed_admin = generate_password_hash('admin123')
        cur.execute("INSERT INTO system_users (email, password_hash, role, school_id) VALUES (%s, %s, %s, %s)", ('admin@school.com', hashed_admin, 'admin', new_school_id))
        
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": "Multi-Tenant SaaS Engine Initialized!"})

# --- 4. AUTHENTICATION ---
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

# --- 5. SUPER ADMIN CONTROL ROOM ---
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

# --- 6. MULTI-TENANT ACADEMICS & FINANCE ---
@app.route('/api/students', methods=['GET', 'POST'])
@login_required
@require_active_subscription
def manage_students():
    conn = get_db_connection()
    cur = conn.cursor()
    if request.method == 'POST':
        data = request.get_json()
        cur.execute("INSERT INTO students (school_id, first_name, last_name, guardian_name, guardian_contact) VALUES (%s, %s, %s, %s, %s) RETURNING student_id", 
                    (current_user.school_id, data.get('first_name'), data.get('last_name'), data.get('guardian_name'), data.get('guardian_contact')))
        new_id = cur.fetchone()['student_id']
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"message": f"Student Enrolled! New ID: {new_id}"}), 201
    elif request.method == 'GET':
        cur.execute("SELECT student_id, first_name, last_name, guardian_contact FROM students WHERE school_id = %s ORDER BY student_id DESC", (current_user.school_id,))
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
    cur.execute("INSERT INTO fees (school_id, student_id, description, amount_due) VALUES (%s, %s, %s, %s) RETURNING fee_id", 
                (current_user.school_id, data.get('student_id'), data.get('description'), data.get('amount_due')))
    new_id = cur.fetchone()['fee_id']
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Bill issued! Reference Fee ID: {new_id}"}), 201

# --- 7. THE SAAS FRONTEND ---
@app.route('/dashboard')
def dashboard():
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ghana SMS | Enterprise Portal</title>
        <style>
            :root { --primary: #0f4c81; --secondary: #f4f7f6; --accent: #28a745; --text: #333; --danger: #dc3545;}
            body { font-family: 'Segoe UI', system-ui, sans-serif; background-color: var(--secondary); margin: 0; display: flex; color: var(--text); }
            .sidebar { width: 250px; background: var(--primary); color: white; min-height: 100vh; padding: 20px; box-sizing: border-box; position: fixed; }
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
            .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            #toast { display: none; position: fixed; bottom: 30px; right: 30px; padding: 15px 25px; color: white; background: var(--accent); border-radius: 5px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); z-index: 1000; font-weight: bold; }
            table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.95rem; margin-top: 15px; }
            th { background: var(--primary); color: white; padding: 12px; }
            td { padding: 12px; border-bottom: 1px solid #eee; }
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
                {% else %}
                    <button onclick="document.getElementById('admissions-section').scrollIntoView()">Admissions Desk</button>
                    <button onclick="document.getElementById('finance-section').scrollIntoView()">Financial Desk</button>
                {% endif %}
                <br><br><button class="btn-danger" onclick="logout()">Secure Logout</button>
            {% else %}
                <button class="btn-success" onclick="sendAction('/api/setup_db', {}, true)">1. Sync SaaS Database</button>
            {% endif %}
        </div>

        <div class="main-content">
            <h1>Platform Dashboard</h1>

            {% if not current_user.is_authenticated %}
            <div class="card" style="max-width: 400px; margin: 0 auto;">
                <h3>System Login</h3>
                <input type="email" id="email" placeholder="Email Address">
                <input type="password" id="pass" placeholder="Password">
                <button class="btn" onclick="login()">Login</button>
            </div>
            
            {% elif current_user.role == 'superadmin' %}
            <div class="card">
                <h3>Global Tenant Control Room</h3>
                <p>Monitor school subscriptions and process yearly contract renewals.</p>
                <button class="btn" onclick="loadSchools()">Refresh Tenant List</button>
                <div id="school-container"></div>
            </div>

            {% else %}
            <div id="admissions-section" class="card grid-2">
                <div>
                    <h3>Enroll New Student</h3>
                    <input type="text" id="sFirst" placeholder="First Name">
                    <input type="text" id="sLast" placeholder="Last Name">
                    <input type="text" id="sGName" placeholder="Guardian Name">
                    <input type="text" id="sGContact" placeholder="Guardian Contact">
                    <button class="btn btn-success" onclick="sendAction('/api/students', {first_name: document.getElementById('sFirst').value, last_name: document.getElementById('sLast').value, guardian_name: document.getElementById('sGName').value, guardian_contact: document.getElementById('sGContact').value})">Register Student</button>
                </div>
                <div>
                    <h3>Student Directory</h3>
                    <button class="btn" onclick="loadRoster()">Load Enrolled Roster</button>
                    <div id="roster-container"></div>
                </div>
            </div>
            <div id="finance-section" class="card">
                <h3>1. Issue Bill</h3>
                <input type="number" id="bStuId" placeholder="Student ID">
                <input type="number" id="bAmount" placeholder="Amount Due (GHS)">
                <input type="text" id="bDesc" placeholder="Description">
                <button class="btn btn-success" onclick="sendAction('/api/fees/bill', {student_id: document.getElementById('bStuId').value, amount_due: document.getElementById('bAmount').value, description: document.getElementById('bDesc').value})">Issue Bill</button>
            </div>
            {% endif %}
        </div>

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
                    if (res.ok) showToast(data.message);
                    else if (res.status === 402) showToast(data.error, true); // Catches the Bouncer!
                    else showToast(data.error || "Error", true);
                    if (endpoint === '/api/setup_db') loadSchools();
                } catch(e) { showToast("Connection failed", true); }
            }

            async function loadRoster() {
                const res = await fetch('/api/students');
                const data = await res.json();
                if (res.status === 402) { showToast(data.error, true); return; } // Bouncer hits here too!
                let html = '<table><tr><th>ID</th><th>First</th><th>Last</th></tr>';
                data.data.forEach(s => html += `<tr><td>${s.student_id}</td><td>${s.first_name}</td><td>${s.last_name}</td></tr>`);
                html += '</table>';
                document.getElementById('roster-container').innerHTML = html;
            }

            async function loadSchools() {
                const res = await fetch('/api/superadmin/schools');
                if(!res.ok) return;
                const data = await res.json();
                let html = '<table><tr><th>ID</th><th>School Name</th><th>Expiry Date</th><th>Status</th><th>Action</th></tr>';
                data.data.forEach(s => {
                    const statusColor = s.status === 'Active' ? 'green' : 'red';
                    html += `<tr>
                        <td>${s.school_id}</td><td>${s.school_name}</td><td>${s.expiry_date}</td>
                        <td style="color:${statusColor}; font-weight:bold;">${s.status}</td>
                        <td><button class="btn btn-success" onclick="sendAction('/api/superadmin/renew/${s.school_id}', {})">Renew 1 Year</button></td>
                    </tr>`;
                });
                html += '</table>';
                document.getElementById('school-container').innerHTML = html;
            }
            if (document.getElementById('school-container')) loadSchools();
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, current_user=current_user)

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
