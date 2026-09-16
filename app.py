import os
import psycopg2
from flask import Flask, jsonify, request, render_template_string
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from auth_models import User
from db_config import get_db_connection
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secure-key')

login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

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

# --- 1. SYSTEM & DATABASE SETUP (ALL TABLES) ---
@app.route('/api/setup_db')
def setup_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Core
    cur.execute("CREATE TABLE IF NOT EXISTS students (student_id SERIAL PRIMARY KEY, first_name VARCHAR(100) NOT NULL, last_name VARCHAR(100) NOT NULL, guardian_name VARCHAR(100) NOT NULL, guardian_contact VARCHAR(20) NOT NULL, enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS classes (class_id SERIAL PRIMARY KEY, class_name VARCHAR(50) NOT NULL UNIQUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS class_enrollments (enrollment_id SERIAL PRIMARY KEY, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, class_id INTEGER REFERENCES classes(class_id) ON DELETE CASCADE, academic_year VARCHAR(9) NOT NULL)")
    
    # Academics & Attendance
    cur.execute("CREATE TABLE IF NOT EXISTS subjects (subject_id SERIAL PRIMARY KEY, subject_name VARCHAR(100) NOT NULL UNIQUE)")
    cur.execute("CREATE TABLE IF NOT EXISTS grades (grade_id SERIAL PRIMARY KEY, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, subject_id INTEGER REFERENCES subjects(subject_id) ON DELETE CASCADE, score INTEGER NOT NULL, waec_grade VARCHAR(2) NOT NULL, academic_year VARCHAR(9) NOT NULL, term VARCHAR(20) NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS attendance (attendance_id SERIAL PRIMARY KEY, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, record_date DATE NOT NULL, status VARCHAR(10) NOT NULL, UNIQUE(student_id, record_date))")
    
    # Financial Ledger
    cur.execute("CREATE TABLE IF NOT EXISTS fees (fee_id SERIAL PRIMARY KEY, student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE, description VARCHAR(255) NOT NULL, amount_due DECIMAL(10, 2) NOT NULL, date_issued TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    cur.execute("CREATE TABLE IF NOT EXISTS payments (payment_id SERIAL PRIMARY KEY, fee_id INTEGER REFERENCES fees(fee_id) ON DELETE CASCADE, amount_paid DECIMAL(10, 2) NOT NULL, payment_method VARCHAR(50) NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": "Full Enterprise Database Initialized (Including Financial Ledger)!"})

# --- 2. AUTHENTICATION ---
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id, email, password_hash, role FROM system_users WHERE email = %s", (data.get('email'),))
    user_data = cur.fetchone()
    cur.close(); conn.close()
    if user_data and check_password_hash(user_data['password_hash'], data.get('password')):
        user = User(user_data['user_id'], user_data['email'], user_data['role'])
        login_user(user)
        return jsonify({"message": f"Welcome back, {user.role}!"})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Logged out successfully"})

# --- 3. CORE & ACADEMIC ROUTES ---
@app.route('/api/students', methods=['GET'])
@login_required
def get_students():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT student_id, first_name, last_name, guardian_contact FROM students ORDER BY student_id DESC")
    students = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"data": students})

@app.route('/api/grades', methods=['POST'])
@login_required
def add_grade():
    data = request.get_json()
    score = int(data.get('score'))
    waec = get_waec_grade(score)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO grades (student_id, subject_id, score, waec_grade, academic_year, term) VALUES (%s, %s, %s, %s, %s, %s)",
        (data.get('student_id'), data.get('subject_id'), score, waec, data.get('academic_year'), data.get('term')))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Score {score} ({waec}) recorded!"})

@app.route('/api/report_card/<int:student_id>', methods=['GET'])
@login_required
def get_report_card(student_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT sub.subject_name, g.score, g.waec_grade, g.term FROM grades g JOIN subjects sub ON g.subject_id = sub.subject_id WHERE g.student_id = %s", (student_id,))
    grades = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"student_id": student_id, "grades": grades})

# --- 4. FINANCIAL ROUTES ---
@app.route('/api/fees/bill', methods=['POST'])
@login_required
def bill_student():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    data = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO fees (student_id, description, amount_due) VALUES (%s, %s, %s) RETURNING fee_id", 
                (data.get('student_id'), data.get('description'), data.get('amount_due')))
    new_id = cur.fetchone()['fee_id']
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Bill issued! Fee ID: {new_id}"}), 201

@app.route('/api/fees/pay', methods=['POST'])
@login_required
def log_payment():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    data = request.get_json()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO payments (fee_id, amount_paid, payment_method) VALUES (%s, %s, %s) RETURNING payment_id", 
                (data.get('fee_id'), data.get('amount_paid'), data.get('payment_method')))
    pid = cur.fetchone()['payment_id']
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Payment of {data.get('amount_paid')} logged via {data.get('payment_method')}! Receipt ID: {pid}"}), 201

@app.route('/api/statement/<int:student_id>', methods=['GET'])
@login_required
def get_statement(student_id):
    conn = get_db_connection()
    cur = conn.cursor()
    # Complex SQL to calculate total owed vs total paid
    query = """
        SELECT f.fee_id, f.description, f.amount_due, 
               COALESCE(SUM(p.amount_paid), 0) as total_paid,
               (f.amount_due - COALESCE(SUM(p.amount_paid), 0)) as remaining_balance
        FROM fees f
        LEFT JOIN payments p ON f.fee_id = p.fee_id
        WHERE f.student_id = %s
        GROUP BY f.fee_id, f.description, f.amount_due
    """
    cur.execute(query, (student_id,))
    statement = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"student_id": student_id, "statement": statement}), 200


# --- 5. THE REAL FRONTEND ---
@app.route('/dashboard')
def dashboard():
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ghana SMS | Admin Dashboard</title>
        <style>
            :root { --primary: #0f4c81; --secondary: #f4f7f6; --accent: #28a745; --text: #333; }
            body { font-family: 'Segoe UI', system-ui, sans-serif; background-color: var(--secondary); margin: 0; display: flex; color: var(--text); }
            .sidebar { width: 250px; background: var(--primary); color: white; min-height: 100vh; padding: 20px; box-sizing: border-box; position: fixed; }
            .sidebar h2 { margin-top: 0; border-bottom: 1px solid rgba(255,255,255,0.2); padding-bottom: 10px; font-size: 1.2rem; }
            .sidebar button { background: rgba(255,255,255,0.1); color: white; border: none; padding: 12px; width: 100%; text-align: left; margin-bottom: 5px; border-radius: 4px; cursor: pointer; transition: 0.3s; }
            .sidebar button:hover { background: rgba(255,255,255,0.2); }
            
            .main-content { margin-left: 250px; flex: 1; padding: 40px; box-sizing: border-box; min-height: 100vh; }
            .card { background: white; padding: 25px; border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 20px; }
            h3 { margin-top: 0; color: var(--primary); border-bottom: 2px solid #eee; padding-bottom: 8px;}
            input, select { width: 100%; padding: 10px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
            .btn { background: var(--primary); color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; margin-bottom: 10px;}
            .btn:hover { background: #0c3e69; }
            .btn-success { background: var(--accent); }
            .btn-success:hover { background: #218838; }
            .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            pre { background: #1e1e1e; color: #00ff00; padding: 15px; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; }
        </style>
    </head>
    <body>
        <div class="sidebar">
            <h2>School Engine</h2>
            <button onclick="document.getElementById('auth-section').scrollIntoView()">1. Authentication</button>
            <button onclick="document.getElementById('grades-section').scrollIntoView()">2. Grading</button>
            <button onclick="document.getElementById('finance-section').scrollIntoView()">3. Financial Desk</button>
            <br><br>
            <button class="btn-success" onclick="testSetup()">Initialize Database</button>
            <button style="background: #dc3545;" onclick="sendPost('/api/logout', {})">Secure Logout</button>
        </div>

        <div class="main-content">
            <h1>Administration Dashboard</h1>

            <div id="auth-section" class="card grid-2">
                <div>
                    <h3>System Login</h3>
                    <input type="email" id="email" placeholder="Email">
                    <input type="password" id="pass" placeholder="Password">
                    <button class="btn" onclick="sendPost('/api/login', {email: document.getElementById('email').value, password: document.getElementById('pass').value})">Login</button>
                </div>
                <div>
                    <h3>Student Roster</h3>
                    <button class="btn" onclick="fetchData('/api/students')">Load All Students</button>
                </div>
            </div>

            <div id="grades-section" class="card grid-2">
                <div>
                    <h3>Record Exam Grade</h3>
                    <input type="number" id="gStuId" placeholder="Student ID">
                    <input type="number" id="gSubId" placeholder="Subject ID">
                    <input type="number" id="gScore" placeholder="Score (0-100)">
                    <input type="text" id="gTerm" placeholder="Term (e.g. Term 1)">
                    <input type="text" id="gYear" placeholder="Year (e.g. 2026)">
                    <button class="btn" onclick="sendPost('/api/grades', {student_id: document.getElementById('gStuId').value, subject_id: document.getElementById('gSubId').value, score: document.getElementById('gScore').value, term: document.getElementById('gTerm').value, academic_year: document.getElementById('gYear').value})">Save Score & Calc WAEC</button>
                </div>
                <div>
                    <h3>Academic Report Card</h3>
                    <input type="number" id="repId" placeholder="Student ID">
                    <button class="btn" onclick="fetchData('/api/report_card/' + document.getElementById('repId').value)">Generate Term Report</button>
                </div>
            </div>

            <!-- THE NEW FINANCIAL ENGINE -->
            <div id="finance-section" class="card grid-2">
                <div>
                    <h3>1. Issue Bill</h3>
                    <input type="number" id="bStuId" placeholder="Student ID">
                    <input type="number" id="bAmount" placeholder="Amount Due (GHS)">
                    <input type="text" id="bDesc" placeholder="Description (e.g. Term 1 Fees)">
                    <button class="btn btn-success" onclick="sendPost('/api/fees/bill', {student_id: document.getElementById('bStuId').value, amount_due: document.getElementById('bAmount').value, description: document.getElementById('bDesc').value})">Issue Bill</button>
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
                    <button class="btn btn-success" onclick="sendPost('/api/fees/pay', {fee_id: document.getElementById('pFeeId').value, amount_paid: document.getElementById('pAmount').value, payment_method: document.getElementById('pMethod').value})">Log Payment</button>
                </div>
            </div>

            <div class="card">
                <h3>Generate Financial Statement</h3>
                <input type="number" id="statStuId" placeholder="Student ID">
                <button class="btn" onclick="fetchData('/api/statement/' + document.getElementById('statStuId').value)">Calculate Outstanding Balance</button>
            </div>

            <div class="card">
                <h3>System Console Output</h3>
                <pre id="output">System data will appear here...</pre>
            </div>
        </div>

        <script>
            async function testSetup() {
                const res = await fetch('/api/setup_db');
                document.getElementById('output').innerText = await res.text();
            }
            async function sendPost(endpoint, payload) {
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                handleResponse(res);
            }
            async function fetchData(endpoint) {
                const res = await fetch(endpoint);
                handleResponse(res);
            }
            async function handleResponse(res) {
                try {
                    const data = await res.json();
                    document.getElementById('output').innerText = JSON.stringify(data, null, 4);
                } catch (e) {
                    document.getElementById('output').innerText = await res.text();
                }
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)

if __name__ == '__main__':
    app.run(debug=True)
