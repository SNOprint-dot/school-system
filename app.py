import os
import psycopg2
from flask import Flask, jsonify, request
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from auth_models import User
from db_config import get_db_connection

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-key')

login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@app.route('/')
def home():
    return jsonify({"message": "Ghana School Management System API is Live!"})

# --- 1. SYSTEM & DATABASE SETUP ---
@app.route('/api/setup_db')
def setup_db():
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Students Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_id SERIAL PRIMARY KEY,
            first_name VARCHAR(100) NOT NULL,
            last_name VARCHAR(100) NOT NULL,
            guardian_name VARCHAR(100) NOT NULL,
            guardian_contact VARCHAR(20) NOT NULL,
            enrollment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 2. Classes Table (e.g., JHS 1, JHS 2)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS classes (
            class_id SERIAL PRIMARY KEY,
            class_name VARCHAR(50) NOT NULL UNIQUE
        )
    """)
    
    # 3. Enrollments Table (Links Students to Classes)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS class_enrollments (
            enrollment_id SERIAL PRIMARY KEY,
            student_id INTEGER REFERENCES students(student_id) ON DELETE CASCADE,
            class_id INTEGER REFERENCES classes(class_id) ON DELETE CASCADE,
            academic_year VARCHAR(9) NOT NULL
        )
    """)
    
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"message": "Relational database tables successfully built in Neon!"})

# --- 2. AUTHENTICATION & ROLES ---
@app.route('/api/register_admin', methods=['POST'])
def register_admin():
    data = request.get_json()
    email, password = data.get('email'), data.get('password')
    if not email or not password: return jsonify({"error": "Missing data"}), 400

    hashed = generate_password_hash(password)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING user_id", (email, hashed, 'admin'))
        uid = cur.fetchone()['user_id']
        conn.commit()
        return jsonify({"message": f"Admin created! ID: {uid}"}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Email exists."}), 409
    finally:
        cur.close(); conn.close()

@app.route('/api/register_teacher', methods=['POST'])
@login_required
def register_teacher():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    
    data = request.get_json()
    email, password = data.get('email'), data.get('password')
    hashed = generate_password_hash(password)
    
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO system_users (email, password_hash, role) VALUES (%s, %s, %s) RETURNING user_id", (email, hashed, 'teacher'))
        uid = cur.fetchone()['user_id']
        conn.commit()
        return jsonify({"message": f"Teacher account created! ID: {uid}"}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({"error": "Email exists."}), 409
    finally:
        cur.close(); conn.close()

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
        return jsonify({"message": f"Logged in as {user.role}!", "role": user.role})
    return jsonify({"error": "Invalid credentials"}), 401

# --- 3. STUDENT MANAGEMENT (CRUD) ---
@app.route('/api/enroll_student', methods=['POST'])
@login_required
def enroll_student():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    data = request.get_json()
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO students (first_name, last_name, guardian_name, guardian_contact) VALUES (%s, %s, %s, %s) RETURNING student_id",
        (data.get('first_name'), data.get('last_name'), data.get('guardian_name'), data.get('guardian_contact'))
    )
    new_id = cur.fetchone()['student_id']
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Student enrolled. ID: {new_id}"}), 201

@app.route('/api/students', methods=['GET'])
@login_required
def get_students():
    # Both Admins and Teachers can view the roster
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT student_id, first_name, last_name, guardian_name, guardian_contact, enrollment_date::text FROM students ORDER BY student_id DESC")
    students = cur.fetchall()
    cur.close(); conn.close()
    return jsonify({"status": "success", "data": students}), 200

@app.route('/api/students/<int:student_id>', methods=['DELETE'])
@login_required
def delete_student(student_id):
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM students WHERE student_id = %s RETURNING student_id", (student_id,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close(); conn.close()
    if deleted: return jsonify({"message": f"Student {student_id} deleted."}), 200
    return jsonify({"error": "Not found"}), 404

# --- 4. ACADEMIC STRUCTURE ---
@app.route('/api/classes', methods=['POST', 'GET'])
@login_required
def manage_classes():
    conn = get_db_connection()
    cur = conn.cursor()
    
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
        class_name = request.get_json().get('class_name')
        try:
            cur.execute("INSERT INTO classes (class_name) VALUES (%s) RETURNING class_id", (class_name,))
            cid = cur.fetchone()['class_id']
            conn.commit()
            return jsonify({"message": f"Class '{class_name}' created! ID: {cid}"}), 201
        except:
            return jsonify({"error": "Class already exists or error occurred."}), 400
        finally:
            cur.close(); conn.close()
            
    elif request.method == 'GET':
        cur.execute("SELECT * FROM classes")
        classes = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"classes": classes}), 200

@app.route('/api/assign_class', methods=['POST'])
@login_required
def assign_class():
    if current_user.role != 'admin': return jsonify({"error": "Admin only."}), 403
    data = request.get_json()
    student_id, class_id, year = data.get('student_id'), data.get('class_id'), data.get('academic_year')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO class_enrollments (student_id, class_id, academic_year) VALUES (%s, %s, %s) RETURNING enrollment_id",
        (student_id, class_id, year)
    )
    eid = cur.fetchone()['enrollment_id']
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"message": f"Student {student_id} assigned to Class {class_id} for {year}!"}), 201


# --- 5. ENHANCED DASHBOARD FRONTEND ---
@app.route('/test_ui')
def test_ui():
    return """
    <html>
        <head>
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f0f2f5; margin: 0; padding: 20px; }
                .container { max-width: 900px; margin: auto; }
                h1 { color: #1a73e8; text-align: center; }
                .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
                .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
                .card h3 { margin-top: 0; color: #333; border-bottom: 2px solid #eee; padding-bottom: 5px; }
                input, button { width: 100%; padding: 10px; margin: 5px 0; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
                button { background: #1a73e8; color: white; border: none; cursor: pointer; font-weight: bold; }
                button:hover { background: #1557b0; }
                button.danger { background: #dc3545; }
                button.danger:hover { background: #c82333; }
                pre { background: #282c34; color: #61dafb; padding: 15px; border-radius: 8px; overflow-x: auto; font-size: 14px; }
                .setup-btn { background: #ff9800; margin-bottom: 20px;}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>SMS Control Dashboard</h1>
                <button class="setup-btn" onclick="testSetup()">1. INITIALIZE DATABASE (Click First)</button>
                
                <div class="grid">
                    <!-- Auth Section -->
                    <div class="card">
                        <h3>Authentication</h3>
                        <input type="email" id="email" placeholder="Email (admin or teacher)">
                        <input type="password" id="pass" placeholder="Password">
                        <button onclick="sendPost('/api/login', {email: document.getElementById('email').value, password: document.getElementById('pass').value})">Login</button>
                        <hr>
                        <input type="email" id="tEmail" placeholder="Teacher Email">
                        <input type="password" id="tPass" placeholder="Teacher Password">
                        <button onclick="sendPost('/api/register_teacher', {email: document.getElementById('tEmail').value, password: document.getElementById('tPass').value})">Register Teacher (Admin Only)</button>
                    </div>

                    <!-- Academic Section -->
                    <div class="card">
                        <h3>Academic Structure</h3>
                        <input type="text" id="className" placeholder="Class Name (e.g., JHS 1)">
                        <button onclick="sendPost('/api/classes', {class_name: document.getElementById('className').value})">Create Class</button>
                        <button onclick="fetchData('/api/classes')">View All Classes</button>
                        <hr>
                        <input type="number" id="a_sId" placeholder="Student ID">
                        <input type="number" id="a_cId" placeholder="Class ID">
                        <input type="text" id="a_year" placeholder="Academic Year (e.g. 2026/2027)">
                        <button onclick="sendPost('/api/assign_class', {student_id: document.getElementById('a_sId').value, class_id: document.getElementById('a_cId').value, academic_year: document.getElementById('a_year').value})">Assign Student to Class</button>
                    </div>

                    <!-- Student Section -->
                    <div class="card">
                        <h3>Student Operations</h3>
                        <button onclick="fetchData('/api/students')">View Student Roster</button>
                        <hr>
                        <input type="number" id="delId" placeholder="Student ID to Delete">
                        <button class="danger" onclick="sendDel('/api/students/' + document.getElementById('delId').value)">Delete Student (Admin Only)</button>
                    </div>
                </div>

                <pre id="output">System output will appear here...</pre>
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
                async function sendDel(endpoint) {
                    const res = await fetch(endpoint, { method: 'DELETE' });
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

if __name__ == '__main__':
    app.run(debug=True)
